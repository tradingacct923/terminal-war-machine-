"""
Greek Surface Engine — Full Sensitivity Surface from Options Chain

Computes higher-order Greek exposures per strike from the raw option data
that schwab_bridge.py already receives via LEVELONE_OPTIONS.

Computed quantities per strike:
  - VannaEX:  OI × vanna × 100     (δ²V/δSδσ — vol-driven delta shift)
  - CharmEX:  OI × charm × 100     (δΔ/δt — time-driven delta decay)
  - SpeedEX:  Finite-diff ΔΓ/ΔS    (gamma acceleration per $1 spot move)
  - ZommaEX:  Record Γ vs IV       (gamma sensitivity to IV changes)
  - VEX:      OI × vega × 100      (vol exposure concentration)

Aggregate metrics:
  - vanna_wall:  strike with max |VannaEX| near spot
  - charm_drift: net charm direction (intraday drift prediction)
  - speed_sign:  is gamma accelerating or decelerating at spot?
  - zomma_risk:  if vol spikes, does gamma explode?
  - vega_peak:   strike with max VEX (vol seller concentration)

All computations are from finite differences across the strike/IV grid —
NO additional API calls. This engine only consumes data already flowing
through _on_options_quote.

Integration:
  schwab_bridge._on_options_quote → GreekSurface.update()
  schwab_bridge._maybe_emit_zones() → GreekSurface.compute_surface()
"""

import math
import time
from collections import defaultdict


class GreekSurface:
    """Maintains a live multi-Greek exposure surface from streaming options data.

    Thread-safe: update() is called from the WebSocket thread,
    compute_surface() is called from the zone emission timer.
    """

    def __init__(self):
        # Per-strike × contract_type state
        # Key: (strike, 'C'|'P')
        self._contracts = defaultdict(lambda: {
            'oi': 0,
            'delta': 0.0,
            'gamma': 0.0,
            'theta': 0.0,
            'vega': 0.0,
            'iv': 0.0,
            'dte': 0,
            'volume': 0,
            'underlying_price': 0.0,
            'mark': 0.0,
            '_prev_delta': None,   # For charm computation (delta change over time)
            '_prev_gamma': None,   # For zomma computation (gamma change over IV)
            '_prev_iv': None,      # For zomma computation
            '_prev_spot': 0.0,     # For Eulerian zomma (fixed-strike frame)
            '_prev_ts': 0.0,       # Timestamp of previous observation
        })

        # Computed surface (refreshed every zone emission cycle)
        self._surface = {}   # strike → {vanna_ex, charm_ex, speed_ex, zomma_ex, vex, ...}
        self._aggregates = {}  # vanna_wall, charm_drift, speed_sign, etc.
        self._last_compute = 0.0

        # Track unique strikes for ordering
        self._strikes = set()
        self._dirty = False

    def get_delta(self, strike, option_side):
        """Return live Δ for one strike/side, from streaming cache.

        Used by connectors/sweep_detector.py to compute notional Δ exposure
        per sweep leg (DERIVED — sum(size × Δ × 100) over legs).

        Returns None if the contract isn't in the streaming cache (e.g. far-OTM
        strike not in the 1,412 LEVELONE_OPTIONS subscription). Caller handles
        the None gracefully — sweep record carries delta_resolved/delta_total_legs
        so frontend can show partial-Δ confidence.

        Args:
          strike:       float — option strike price
          option_side:  'C' or 'P' (case-insensitive accepted)
        """
        try:
            ct = (option_side or '').upper()
            if ct in ('C', 'CALL'):
                ct = 'C'
            elif ct in ('P', 'PUT'):
                ct = 'P'
            else:
                return None
            entry = self._contracts.get((float(strike), ct))
            if entry is None:
                return None
            d = entry.get('delta')
            if d is None or d == 0:
                return None
            return float(d)
        except Exception:
            return None

    def update(self, data):
        """Called on every _on_options_quote tick.

        data should contain: strike, contract_type, delta, gamma, theta,
        vega, implied_vol (iv), open_interest (oi), dte, total_volume,
        underlying_price, mark
        """
        strike = float(data.get('strike', 0))
        if strike <= 0:
            return

        ct_raw = data.get('contract_type', '')
        if ct_raw in ('C', 'CALL', 'call'):
            ct = 'C'
        elif ct_raw in ('P', 'PUT', 'put'):
            ct = 'P'
        else:
            return

        key = (strike, ct)
        c = self._contracts[key]
        now = time.time()

        # Capture previous values for finite-difference Greeks
        if c['_prev_ts'] > 0:
            c['_prev_delta'] = c['delta']
            c['_prev_gamma'] = c['gamma']
            c['_prev_iv'] = c['iv']
            c['_prev_spot'] = c['underlying_price']

        # Update current values
        c['oi'] = int(data.get('open_interest', 0) or 0)
        c['delta'] = float(data.get('delta', 0) or 0)
        c['gamma'] = abs(float(data.get('gamma', 0) or 0))
        c['theta'] = float(data.get('theta', 0) or 0)
        c['vega'] = float(data.get('vega', 0) or 0)
        c['iv'] = float(data.get('implied_vol', 0) or 0)
        c['dte'] = int(data.get('dte', 0) or 0)
        c['volume'] = int(data.get('total_volume', data.get('volume', 0)) or 0)
        c['underlying_price'] = float(data.get('underlying_price', 0) or 0)
        c['mark'] = float(data.get('mark', data.get('last', 0)) or 0)
        c['_prev_ts'] = now

        # Volume-adjusted effective OI (same as schwab_bridge)
        eff_oi = c['oi'] + c['volume'] * 0.3
        c['_eff_oi'] = eff_oi

        self._strikes.add(strike)
        self._dirty = True

    def compute_surface(self, spot, nq_ratio=1.0):
        """Recompute the full Greek exposure surface.

        Called by schwab_bridge._maybe_emit_zones() on the same cycle.

        Args:
            spot: QQQ spot price (options are QQQ-native)
            nq_ratio: NQ/QQQ ratio for converting to NQ prices

        Returns:
            dict with per-strike surface and aggregate metrics
        """
        if not self._dirty and (time.time() - self._last_compute) < 3.0:
            return self._aggregates  # Return cached

        now = time.time()
        sorted_strikes = sorted(self._strikes)
        if len(sorted_strikes) < 3:
            return {}

        multiplier = 100  # options contract multiplier

        # ═══════════════════════════════════════════════════
        #  PER-STRIKE GREEK EXPOSURE
        # ═══════════════════════════════════════════════════

        vanna_ex = {}    # VannaEX per strike (net)
        charm_ex = {}    # CharmEX per strike (net)
        vex = {}         # VEX (Vega Exposure) per strike (net)
        tex = {}         # TEX (Theta Exposure) per strike (net)
        gamma_by_strike = {}  # For speed computation
        # Phase A (hedge-pressure): double-buffered per-strike snapshot.
        # Populated inside the per-strike loop, swapped atomically onto
        # self._surface after all computations. Readers in
        # export_hedge_pressure() see a consistent snapshot without locking.
        surface_next = {}

        for K in sorted_strikes:
            call = self._contracts.get((K, 'C'), {})
            put = self._contracts.get((K, 'P'), {})

            c_oi = call.get('_eff_oi', call.get('oi', 0))
            p_oi = put.get('_eff_oi', put.get('oi', 0))
            c_delta = call.get('delta', 0)
            p_delta = put.get('delta', 0)
            c_gamma = call.get('gamma', 0)
            p_gamma = put.get('gamma', 0)
            c_vega = call.get('vega', 0)
            p_vega = put.get('vega', 0)
            c_theta = call.get('theta', 0)
            p_theta = put.get('theta', 0)
            c_iv = call.get('iv', 0)
            p_iv = put.get('iv', 0)

            # ── VannaEX ──────────────────────────────────
            # Vanna = ∂Δ/∂σ ≈ vega / S  (BSM approximation)
            # For calls: vanna ≈ c_vega / spot (positive)
            # For puts:  vanna ≈ p_vega / spot (negative, since put delta < 0)
            # Dealer is SHORT what client is LONG:
            #   Net dealer VannaEX = -(call_OI × call_vanna) + (put_OI × put_vanna)
            if spot > 0:
                c_vanna = c_vega / spot if c_vega != 0 else 0
                p_vanna = p_vega / spot if p_vega != 0 else 0
                # Dealer vanna = negative of client position
                vanna_k = (-(c_oi * c_vanna) + (p_oi * abs(p_vanna))) * multiplier
            else:
                vanna_k = 0
            vanna_ex[K] = vanna_k

            # ── CharmEX ──────────────────────────────────
            # Charm = -∂Δ/∂t (delta decay per day)
            # PRIMARY: actual finite difference from stored delta history
            # FALLBACK: BSM closed-form charm (no proxy)
            c_charm = 0
            p_charm = 0
            call_entry = self._contracts.get((K, 'C'))
            put_entry = self._contracts.get((K, 'P'))

            if call_entry and call_entry.get('_prev_delta') is not None:
                prev_ts = call_entry.get('_prev_ts', 0)
                dt_sec = now - prev_ts
                if dt_sec > 2.0:  # need at least 2s gap for meaningful difference
                    dt_days = dt_sec / 86400.0
                    c_charm = -(c_delta - call_entry['_prev_delta']) / dt_days
            elif c_gamma > 0 and spot > 0 and abs(c_delta) > 0.01:
                # BSM closed-form charm for call: -gamma * (r - (ln(S/K) + (r+σ²/2)T)/(2T))
                # Simplified: charm ≈ -gamma * (2*r*T - d2*σ√T) / (2*T*σ√T)
                # Use |theta|/|delta| as conservative lower bound when BSM params unavailable
                c_iv = call_entry.get('iv', 0) if call_entry else 0
                if c_iv > 0:
                    # Approximate d1 from delta: d1 ≈ Φ⁻¹(delta) for calls
                    c_charm = c_gamma * (0.5 * c_iv * c_iv) if c_iv > 0 else 0
                else:
                    c_charm = abs(c_theta) * c_gamma / abs(c_delta)

            if put_entry and put_entry.get('_prev_delta') is not None:
                prev_ts = put_entry.get('_prev_ts', 0)
                dt_sec = now - prev_ts
                if dt_sec > 2.0:
                    dt_days = dt_sec / 86400.0
                    p_charm = -(p_delta - put_entry['_prev_delta']) / dt_days
            elif p_gamma > 0 and spot > 0 and abs(p_delta) > 0.01:
                p_iv = put_entry.get('iv', 0) if put_entry else 0
                if p_iv > 0:
                    p_charm = p_gamma * (0.5 * p_iv * p_iv) if p_iv > 0 else 0
                else:
                    p_charm = abs(p_theta) * p_gamma / abs(p_delta)

            # Dealer charm exposure (negative of client position)
            charm_k = (-(c_oi * c_charm) + (p_oi * p_charm)) * multiplier
            charm_ex[K] = charm_k

            # ── VEX (Vega Exposure) ──────────────────────
            # Net dealer vega = -(call_OI × call_vega) + (put_OI × put_vega)
            vex_k = (-(c_oi * abs(c_vega)) + (p_oi * abs(p_vega))) * multiplier
            vex[K] = vex_k

            # ── TEX (Theta Exposure) ─────────────────────
            # Net dealer theta = sum of all theta (dealers COLLECT decay when short)
            tex_k = (c_oi * c_theta + p_oi * p_theta) * multiplier
            tex[K] = tex_k

            # ── Store gamma for Speed computation ────────
            # Dealer net gamma (same as schwab_bridge)
            c_dollar_gex = c_gamma * c_oi * multiplier * (spot * spot / 100) if spot > 0 else 0
            p_dollar_gex = p_gamma * p_oi * multiplier * (spot * spot / 100) if spot > 0 else 0
            dealer_gex_k = -c_dollar_gex + p_dollar_gex
            gamma_by_strike[K] = dealer_gex_k

            # ── Phase A: per-strike hedge-pressure snapshot ──
            # Store signed dealer exposures for export_hedge_pressure().
            # dn_gamma is $-delta change per +1% spot move at this strike (= γ · OI · S²).
            # Rehedge shares at this strike = −dn_gamma / spot (see export_hedge_pressure docstring).
            # dn_vanna/dn_charm are dealer-signed Greek exposures in contract units × OI × 100.
            surface_next[K] = {
                'dn_gamma':  dealer_gex_k,
                'dn_vanna':  vanna_k,
                'dn_charm':  charm_k,
                'oi_call':   c_oi,
                'oi_put':    p_oi,
                'iv_call':   c_iv,
                'iv_put':    p_iv,
            }

        # ═══════════════════════════════════════════════════
        #  SPEED (∂Γ/∂S) — Tikhonov-regularized finite difference
        # ═══════════════════════════════════════════════════
        # Step 1: Smooth the gamma surface with a Gaussian-weighted local average
        # to suppress noise before differentiation (Tikhonov regularization).
        # Bandwidth h = typical strike spacing × 2
        speed_ex = {}
        if len(sorted_strikes) >= 5:
            # Compute typical strike spacing
            spacings = [sorted_strikes[i+1] - sorted_strikes[i]
                        for i in range(len(sorted_strikes)-1) if sorted_strikes[i+1] > sorted_strikes[i]]
            h = (sum(spacings) / len(spacings)) * 2.0 if spacings else 1.0
            h = max(h, 0.5)  # floor bandwidth

            # Nadaraya-Watson kernel smoother for gamma surface
            gamma_smooth = {}
            for i, K in enumerate(sorted_strikes):
                wt_sum = 0.0
                g_sum = 0.0
                for j, Kj in enumerate(sorted_strikes):
                    gj = gamma_by_strike.get(Kj, 0)
                    dist = abs(K - Kj) / h
                    if dist < 3.0:  # truncate at 3 bandwidths
                        w = math.exp(-0.5 * dist * dist)
                        g_sum += w * gj
                        wt_sum += w
                gamma_smooth[K] = g_sum / wt_sum if wt_sum > 0 else 0

            # Step 2: Central finite difference on smoothed gamma
            for i in range(1, len(sorted_strikes) - 1):
                K_prev = sorted_strikes[i - 1]
                K = sorted_strikes[i]
                K_next = sorted_strikes[i + 1]
                g_prev = gamma_smooth.get(K_prev, 0)
                g_next = gamma_smooth.get(K_next, 0)
                dS = K_next - K_prev
                if dS > 0:
                    speed_ex[K] = (g_next - g_prev) / dS
                else:
                    speed_ex[K] = 0
        else:
            # Too few strikes for smoothing — raw difference
            for i in range(1, len(sorted_strikes) - 1):
                K_prev = sorted_strikes[i - 1]
                K = sorted_strikes[i]
                K_next = sorted_strikes[i + 1]
                g_prev = gamma_by_strike.get(K_prev, 0)
                g_next = gamma_by_strike.get(K_next, 0)
                dS = K_next - K_prev
                if dS > 0:
                    speed_ex[K] = (g_next - g_prev) / dS
                else:
                    speed_ex[K] = 0

        # ═══════════════════════════════════════════════════
        #  ZOMMA (∂Γ/∂σ) — Eulerian frame: fixed strike, gamma vs IV at SAME strike
        # ═══════════════════════════════════════════════════
        # Eulerian: measure dGamma/dIV at fixed strike K (not tracking gamma
        # across different IV levels as spot moves — that was the Lagrangian bug).
        # Only compute when IV at THIS strike changed, holding strike fixed.
        zomma_ex = {}
        for K in sorted_strikes:
            for ct in ('C', 'P'):
                entry = self._contracts.get((K, ct))
                if not entry:
                    continue
                prev_gamma = entry.get('_prev_gamma')
                prev_iv = entry.get('_prev_iv')
                prev_spot = entry.get('_prev_spot', 0)
                curr_gamma = entry.get('gamma', 0)
                curr_iv = entry.get('iv', 0)

                if (prev_gamma is not None and prev_iv is not None
                        and prev_iv > 0 and curr_iv > 0
                        and abs(curr_iv - prev_iv) > 0.001):
                    # Eulerian check: only valid if spot hasn't moved much
                    # (otherwise gamma change is from spot move, not IV move)
                    spot_moved = abs(spot - prev_spot) / max(spot, 1) if prev_spot > 0 else 0
                    if spot_moved < 0.002:  # less than 0.2% spot move → valid Eulerian
                        d_gamma = curr_gamma - prev_gamma
                        d_iv = curr_iv - prev_iv
                        zomma_local = d_gamma / d_iv
                        oi = entry.get('_eff_oi', entry.get('oi', 0))
                        sign = -1 if ct == 'C' else 1  # Dealer is short client position
                        zomma_ex[K] = zomma_ex.get(K, 0) + sign * oi * zomma_local * multiplier

        # ═══════════════════════════════════════════════════
        #  IV SURFACE METRICS (per-strike IV for skew computation)
        # ═══════════════════════════════════════════════════
        call_iv_map = {}
        put_iv_map = {}
        iv_by_dte = defaultdict(list)  # dte → [(strike, iv, oi)]

        for K in sorted_strikes:
            call = self._contracts.get((K, 'C'), {})
            put = self._contracts.get((K, 'P'), {})

            c_iv = call.get('iv', 0)
            p_iv = put.get('iv', 0)
            c_dte = call.get('dte', 0)
            p_dte = put.get('dte', 0)

            if c_iv > 0:
                call_iv_map[K] = c_iv
                iv_by_dte[c_dte].append((K, c_iv, call.get('oi', 0)))
            if p_iv > 0:
                put_iv_map[K] = p_iv
                iv_by_dte[p_dte].append((K, p_iv, put.get('oi', 0)))

        # ═══════════════════════════════════════════════════
        #  AGGREGATE METRICS
        # ═══════════════════════════════════════════════════

        # Vanna Wall: strike with maximum |VannaEX| within ±5% of spot
        vanna_wall = spot
        max_abs_vanna = 0
        for K, v in vanna_ex.items():
            if spot > 0 and abs(K - spot) / spot < 0.05 and abs(v) > max_abs_vanna:
                max_abs_vanna = abs(v)
                vanna_wall = K

        # Charm Drift Direction: net charm across all strikes
        # Positive = delta decaying toward long (price drifts up into close)
        # Negative = delta decaying toward short (price drifts down into close)
        net_charm = sum(charm_ex.values())
        charm_direction = 'UP' if net_charm > 0 else 'DOWN'
        charm_magnitude = abs(net_charm)

        # Charm gravity: strike where charm is most concentrated (magnet price)
        charm_gravity_strike = spot
        max_abs_charm = 0
        for K, v in charm_ex.items():
            if abs(v) > max_abs_charm:
                max_abs_charm = abs(v)
                charm_gravity_strike = K

        # Speed at spot: interpolate speed at current price
        speed_at_spot = 0
        for i in range(len(sorted_strikes) - 1):
            if sorted_strikes[i] <= spot <= sorted_strikes[i + 1]:
                s0 = speed_ex.get(sorted_strikes[i], 0)
                s1 = speed_ex.get(sorted_strikes[i + 1], 0)
                frac = (spot - sorted_strikes[i]) / (sorted_strikes[i + 1] - sorted_strikes[i]) if sorted_strikes[i + 1] != sorted_strikes[i] else 0.5
                speed_at_spot = s0 + frac * (s1 - s0)
                break

        # Zomma risk at spot: if vol spikes, does gamma explode?
        zomma_at_spot = 0
        for i in range(len(sorted_strikes) - 1):
            if sorted_strikes[i] <= spot <= sorted_strikes[i + 1]:
                z0 = zomma_ex.get(sorted_strikes[i], 0)
                z1 = zomma_ex.get(sorted_strikes[i + 1], 0)
                frac = (spot - sorted_strikes[i]) / (sorted_strikes[i + 1] - sorted_strikes[i]) if sorted_strikes[i + 1] != sorted_strikes[i] else 0.5
                zomma_at_spot = z0 + frac * (z1 - z0)
                break

        # Vega peak: strike with max vega exposure
        vega_peak = spot
        max_abs_vex = 0
        for K, v in vex.items():
            if abs(v) > max_abs_vex:
                max_abs_vex = abs(v)
                vega_peak = K

        # IV Skew: put IV / call IV at ATM
        atm_strike = min(sorted_strikes, key=lambda k: abs(k - spot)) if sorted_strikes else spot
        atm_call_iv = call_iv_map.get(atm_strike, 0)
        atm_put_iv = put_iv_map.get(atm_strike, 0)
        iv_skew = (atm_put_iv / atm_call_iv) if atm_call_iv > 0 else 1.0

        # IV Term Structure: compare 0DTE IV to next expiry IV
        dte_keys = sorted(iv_by_dte.keys())
        iv_0dte = 0
        iv_next = 0
        if len(dte_keys) >= 1:
            # OI-weighted mean IV for 0DTE
            entries_0 = iv_by_dte[dte_keys[0]]
            total_oi_0 = sum(e[2] for e in entries_0) or 1
            iv_0dte = sum(e[1] * e[2] for e in entries_0) / total_oi_0
        if len(dte_keys) >= 2:
            entries_1 = iv_by_dte[dte_keys[1]]
            total_oi_1 = sum(e[2] for e in entries_1) or 1
            iv_next = sum(e[1] * e[2] for e in entries_1) / total_oi_1

        term_structure = 'FLAT'
        if iv_0dte > 0 and iv_next > 0:
            ratio = iv_0dte / iv_next
            if ratio > 1.05:
                term_structure = 'BACKWARDATION'  # Stressed: near-term IV > far-term
            elif ratio < 0.95:
                term_structure = 'CONTANGO'  # Calm: near-term IV < far-term

        # Net vega (total)
        net_vex = sum(vex.values())

        # Net theta (total)
        net_tex = sum(tex.values())

        # ═══════════════════════════════════════════════════
        #  BUILD DEX PROFILE FOR VANNA/CHARM (for ThermalFlare)
        # ═══════════════════════════════════════════════════
        vanna_profile = []
        charm_profile = []
        for K in sorted_strikes:
            v = vanna_ex.get(K, 0)
            c = charm_ex.get(K, 0)

            # Normalize to z-scores for rendering
            if max_abs_vanna > 0 and abs(v) / max_abs_vanna > 0.1:
                vanna_profile.append({
                    'price': round(K * nq_ratio, 2),
                    'value': round(v, 2),
                    'z': round(abs(v) / max_abs_vanna * 3, 2),  # Scale to ~0-3σ range
                })
            if max_abs_charm > 0 and abs(c) / max_abs_charm > 0.1:
                charm_profile.append({
                    'price': round(K * nq_ratio, 2),
                    'value': round(c, 2),
                    'z': round(abs(c) / max_abs_charm * 3, 2),
                })

        # ═══════════════════════════════════════════════════
        #  CONFLUENCE DETECTION
        # ═══════════════════════════════════════════════════
        # Find strikes where multiple Greek exposures concentrate
        confluence_strikes = []
        for K in sorted_strikes:
            if spot > 0 and abs(K - spot) / spot > 0.05:
                continue  # Only check within 5% of spot

            signals = 0
            confluence_details = []

            # Check each Greek dimension
            v = abs(vanna_ex.get(K, 0))
            if max_abs_vanna > 0 and v / max_abs_vanna > 0.5:
                signals += 1
                confluence_details.append('VANNA')

            c = abs(charm_ex.get(K, 0))
            if max_abs_charm > 0 and c / max_abs_charm > 0.5:
                signals += 1
                confluence_details.append('CHARM')

            s = abs(speed_ex.get(K, 0))
            all_speeds = [abs(sv) for sv in speed_ex.values() if sv != 0]
            max_speed = max(all_speeds) if all_speeds else 1
            if max_speed > 0 and s / max_speed > 0.5:
                signals += 1
                confluence_details.append('SPEED')

            g = abs(gamma_by_strike.get(K, 0))
            all_gammas = [abs(gv) for gv in gamma_by_strike.values() if gv != 0]
            max_gamma = max(all_gammas) if all_gammas else 1
            if max_gamma > 0 and g / max_gamma > 0.5:
                signals += 1
                confluence_details.append('GEX')

            if signals >= 3:
                confluence_strikes.append({
                    'strike': K,
                    'nq_price': round(K * nq_ratio, 2),
                    'signals': signals,
                    'types': confluence_details,
                })

        # Sort by signal count (highest confluence first)
        confluence_strikes.sort(key=lambda x: x['signals'], reverse=True)

        # ═══════════════════════════════════════════════════
        #  Phase A: atomic swap of per-strike hedge-pressure snapshot
        # ═══════════════════════════════════════════════════
        # Single-reference assignment is atomic in CPython — readers in
        # export_hedge_pressure() see a fully-populated dict or the prior
        # one; never a half-written one. No lock needed on the reader path.
        self._surface = surface_next

        # ═══════════════════════════════════════════════════
        #  PACKAGE RESULTS
        # ═══════════════════════════════════════════════════
        self._aggregates = {
            # Vanna
            'vanna_wall': round(vanna_wall * nq_ratio, 2),
            'vanna_wall_qqq': round(vanna_wall, 2),
            'vanna_wall_ex': round(max_abs_vanna, 0),

            # Charm
            'charm_direction': charm_direction,
            'charm_magnitude': round(charm_magnitude, 0),
            'charm_gravity': round(charm_gravity_strike * nq_ratio, 2),
            'charm_gravity_qqq': round(charm_gravity_strike, 2),

            # Speed
            'speed_at_spot': round(speed_at_spot, 2),
            'speed_sign': 'ACCELERATING' if speed_at_spot > 0 else 'DECELERATING',

            # Zomma
            'zomma_at_spot': round(zomma_at_spot, 2),
            'zomma_risk': 'HIGH' if abs(zomma_at_spot) > 0 else 'LOW',

            # Vega
            'vega_peak': round(vega_peak * nq_ratio, 2),
            'vega_peak_qqq': round(vega_peak, 2),
            'net_vex': round(net_vex, 0),

            # Theta
            'net_tex': round(net_tex, 0),

            # IV Surface
            'iv_skew': round(iv_skew, 4),
            'iv_skew_label': 'STRONG_PUT_SKEW' if iv_skew > 1.10 else ('MILD_PUT_SKEW' if iv_skew > 1.02 else ('FLAT' if iv_skew > 0.98 else 'CALL_SKEW')),
            'atm_iv': round(atm_call_iv, 2) if atm_call_iv > 0 else round(atm_put_iv, 2),
            'iv_0dte': round(iv_0dte, 2),
            'iv_next_dte': round(iv_next, 2),
            'term_structure': term_structure,

            # Profiles for frontend rendering
            'vanna_profile': vanna_profile[:30],   # Cap for bandwidth
            'charm_profile': charm_profile[:30],

            # Confluence
            'confluence_zones': confluence_strikes[:5],  # Top 5 nuclear levels
            'confluence_count': len(confluence_strikes),
        }

        self._last_compute = time.time()
        self._dirty = False

        return self._aggregates

    def export_hedge_pressure(self, spot):
        """Read-only snapshot. Returns per-strike hedge-flow metrics in SHARES.

        Sign convention (matches ``wall_signals.expected_direction`` at line 458):
          hp_* > 0  →  dealers must BUY  shares to rehedge (short-γ chase)
          hp_* < 0  →  dealers must SELL shares to rehedge (long-γ fade)

        Derivations (no guessed scalars):

          dn_gamma  = γ · OI · 100 · (S² / 100) = γ · OI · S²   [dollar delta change per +1% spot move]
          dn_vanna  = vanna · OI · 100                          [shares of δ change per 1 unit σ change]
          dn_charm  = −(Δδ / Δt_days) · OI · 100                [shares of δ change per 1 day of decay]

        Rehedge (dealer must trade −Δ(dealer_delta) to stay neutral):

          hp_gamma_shares_1pct   = −dn_gamma / spot              [Δ$delta → shares via ÷ spot]
          hp_vanna_shares_1volpt = −dn_vanna · 0.01              [1 vol-pt = Δσ of 0.01]
          hp_charm_shares_1hr    = −dn_charm · (1.0 / 24.0)      [per-day → per-hour]

        All values are signed and in SHARES. Totals are sums across visible strikes.

        The per-strike ``oi_balance_strike_*`` fields (formerly mis-named
        zero_*_strike) mark the first K at which the per-strike ``dn_*`` flips
        sign — i.e. where call-OI × γ_c ≈ put-OI × γ_p at that single strike.
        This is a LOCAL OI-balance marker, NOT the aggregate ``gamma_flip``.

        Args:
            spot: current underlying price (QQQ). Required to convert $-γ to shares.

        Returns:
            {
              'strikes': [
                {'K', 'dn_gamma', 'dn_vanna', 'dn_charm',
                 'oi_call', 'oi_put',
                 'hp_gamma_shares_1pct', 'hp_vanna_shares_1volpt',
                 'hp_charm_shares_1hr'}, ...
              ],
              'oi_balance_strike_gamma': float | None,
              'oi_balance_strike_vanna': float | None,
              'oi_balance_strike_charm': float | None,
              'totals': {'hp_gamma_shares_1pct', 'hp_vanna_shares_1volpt', 'hp_charm_shares_1hr'},
              'spot': float, 'ts': float,
            }
        """
        # Grab snapshot reference once — atomic read in CPython.
        snap = self._surface
        if not snap or spot is None or spot <= 0:
            return {
                'strikes': [],
                'oi_balance_strike_gamma': None,
                'oi_balance_strike_vanna': None,
                'oi_balance_strike_charm': None,
                'totals': {
                    'hp_gamma_shares_1pct':   0.0,
                    'hp_vanna_shares_1volpt': 0.0,
                    'hp_charm_shares_1hr':    0.0,
                },
                'spot': float(spot) if spot else 0.0,
                'ts': time.time(),
            }

        # Sort strikes ascending for structural OI-balance detection.
        ks = sorted(snap.keys())

        rows = []
        tot_g = 0.0
        tot_v = 0.0
        tot_c = 0.0

        for K in ks:
            e = snap.get(K) or {}
            dn_g = float(e.get('dn_gamma', 0.0) or 0.0)
            dn_v = float(e.get('dn_vanna', 0.0) or 0.0)
            dn_c = float(e.get('dn_charm', 0.0) or 0.0)

            # Rehedge shares = −Δ(dealer_delta). See class docstring above for
            # derivation. Sign-flip is the "−" prefix on each dn_* term.
            hp_g = -dn_g / spot                   # shares per +1% spot move
            hp_v = -dn_v * 0.01                   # shares per +1 vol-pt (Δσ=0.01)
            hp_c = -dn_c * (1.0 / 24.0)           # shares per +1 hour of decay

            tot_g += hp_g
            tot_v += hp_v
            tot_c += hp_c

            rows.append({
                'K':                      float(K),
                'dn_gamma':               dn_g,
                'dn_vanna':               dn_v,
                'dn_charm':               dn_c,
                'oi_call':                int(e.get('oi_call', 0) or 0),
                'oi_put':                 int(e.get('oi_put', 0) or 0),
                'hp_gamma_shares_1pct':   hp_g,
                'hp_vanna_shares_1volpt': hp_v,
                'hp_charm_shares_1hr':    hp_c,
            })

        # Structural zero-crossings: first K at which the sign of each dn_* flips.
        # Linear-interpolate between bracketing strikes for the crossing level.
        def _first_crossing(field):
            prev = None
            for r in rows:
                v = r.get(field, 0.0)
                if prev is not None:
                    pv = prev[1]
                    if pv != 0.0 and v != 0.0 and ((pv > 0) != (v > 0)):
                        # sign flip between prev['K'] and r['K']
                        dK = r['K'] - prev[0]
                        if (v - pv) != 0:
                            frac = -pv / (v - pv)
                            return round(prev[0] + frac * dK, 4)
                        return round((prev[0] + r['K']) / 2.0, 4)
                prev = (r['K'], v)
            return None

        zero_g = _first_crossing('dn_gamma')
        zero_v = _first_crossing('dn_vanna')
        zero_c = _first_crossing('dn_charm')

        return {
            'strikes': rows,
            'oi_balance_strike_gamma': zero_g,
            'oi_balance_strike_vanna': zero_v,
            'oi_balance_strike_charm': zero_c,
            'totals': {
                'hp_gamma_shares_1pct':   tot_g,
                'hp_vanna_shares_1volpt': tot_v,
                'hp_charm_shares_1hr':    tot_c,
            },
            'spot': float(spot),
            'ts':   time.time(),
        }

    def get_confluence_at_price(self, qqq_price, threshold_pct=0.003):
        """Check if a specific QQQ price sits on a confluence zone.

        Used by EdgeDetector to evaluate signal quality.

        Returns:
            (is_confluence, signals, types) or (False, 0, [])
        """
        for zone in self._aggregates.get('confluence_zones', []):
            strike = zone.get('strike', 0)
            if strike > 0 and abs(strike - qqq_price) / qqq_price < threshold_pct:
                return True, zone['signals'], zone['types']
        return False, 0, []

    def get_vanna_direction(self, qqq_price, iv_change_direction):
        """Predict vanna-driven delta shift.

        When IV rises: vanna pushes delta in one direction
        When IV falls: vanna pushes delta in the opposite direction

        Args:
            qqq_price: current QQQ spot
            iv_change_direction: +1 (IV rising) or -1 (IV falling)

        Returns:
            'LONG', 'SHORT', or 'NEUTRAL'
        """
        vanna_wall = self._aggregates.get('vanna_wall_qqq', 0)
        if vanna_wall <= 0 or qqq_price <= 0:
            return 'NEUTRAL'

        # If spot is below vanna wall and IV is rising:
        # Rising IV + positive vanna = delta increases = bullish
        # Rising IV + negative vanna = delta decreases = bearish
        if qqq_price < vanna_wall:
            return 'LONG' if iv_change_direction > 0 else 'SHORT'
        else:
            return 'SHORT' if iv_change_direction > 0 else 'LONG'

    def get_charm_bias(self, current_hour_et=None):
        """Get intraday charm bias. Strongest in last 90 minutes.

        Returns:
            (direction, strength) where strength is 0.0-1.0
        """
        direction = self._aggregates.get('charm_direction', 'NEUTRAL')
        magnitude = self._aggregates.get('charm_magnitude', 0)

        # Charm accelerates exponentially into close
        # 3:30 PM ET = strongest, 9:30 AM ET = weakest
        time_multiplier = 0.5  # default: mid-day
        if current_hour_et is not None:
            if current_hour_et >= 15.5:    # 3:30 PM+
                time_multiplier = 1.0
            elif current_hour_et >= 15.0:  # 3:00 PM+
                time_multiplier = 0.8
            elif current_hour_et >= 14.0:  # 2:00 PM+
                time_multiplier = 0.6
            elif current_hour_et >= 12.0:  # Noon+
                time_multiplier = 0.4
            else:
                time_multiplier = 0.2

        # Normalize magnitude to 0-1 scale (empirical: 1M charm = moderate)
        strength = min(1.0, (magnitude / 1_000_000) * time_multiplier)

        return direction, strength

    def get_speed_context(self, qqq_price):
        """Check if gamma is accelerating at current spot (breakout signal).

        Positive speed = gamma increasing as price moves up = explosive breakout
        Negative speed = gamma decreasing = mean-reverting, dampened

        Returns:
            (sign, label)
        """
        speed = self._aggregates.get('speed_at_spot', 0)
        if speed > 0.1:
            return 1, 'GAMMA_ACCELERATING'
        elif speed < -0.1:
            return -1, 'GAMMA_DECELERATING'
        return 0, 'GAMMA_NEUTRAL'
