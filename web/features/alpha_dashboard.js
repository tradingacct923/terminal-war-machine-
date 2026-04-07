/**
 * Alpha Dashboard
 * Renders a dynamic Chart.js Radar chart visualizing the 8 backend quant frameworks.
 */
(function() {
    'use strict';

    // Same metadata as app.js SIG_META
    const ALPHA_META = {
        shannon_entropy:       { label: 'Chaos (Shannon)',     hi: 8,    color: '#7c5af7' },
        ising_magnetization:   { label: 'Trend (Ising)',       hi: 1,    color: '#28c4f8' },
        reynolds_number:       { label: 'Flow (Reynolds)',     hi: 5000, color: '#1fd17a' },
        lppl_sornette:         { label: 'Bubble (LPPL)',       hi: 1,    color: '#e8435a' },
        powerlaw_tail:         { label: 'Tail Risk (α)',       hi: 6,    color: '#e6b430' },
        transfer_entropy:      { label: 'VIX Flow (Transfer)', hi: 4,    color: '#f07828' },
        percolation_threshold: { label: 'Cascade (Percol)',    hi: 1,    color: '#9b7ef8' },
        mutual_information:    { label: 'Coupling (Mutual)',   hi: 3,    color: '#b06fff' },
    };

    // Store all active instances by their container ID or reference
    const instances = new Map();

    class AlphaDashboardInstance {
        constructor(container) {
            this.container = container;
            this.radarChart = null;

            // Generate unique canvas ID
            const cid = 'alpha-dash-' + Math.random().toString(36).substr(2, 9);

            container.innerHTML = `
                <div class="alpha-dash-wrap" style="width:100%; height:100%; display:flex; flex-direction:column; padding:10px; box-sizing:border-box;">
                    <div style="text-align:center; padding-bottom:8px; display:flex; flex-direction:column; gap:4px">
                        <span style="color:rgba(140,160,200,.8); font-size:11px; font-weight:600; letter-spacing:0.1em;">QUANT INFERENCE ENGINE</span>
                        <span style="color:var(--cyan); font-size:9px; font-family:'JetBrains Mono',monospace;">LIVE REAL-TIME VECTORS</span>
                    </div>
                    <div style="flex:1; position:relative; overflow:hidden;">
                        <canvas id="${cid}"></canvas>
                    </div>
                </div>
            `;

            this.canvasEl = document.getElementById(cid);
            this.initChart();

            // Bind the update method to this instance
            this.updateHandler = this.updateData.bind(this);

            // Listen for live signal data
            if (window.AltarisEvents) {
                window.AltarisEvents.on('data:zone:update', this.updateHandler);
            }
        }

        initChart() {
            const labels = Object.values(ALPHA_META).map(m => m.label);
            const dataVals = Array(labels.length).fill(0);
            
            const bgColor = 'rgba(124, 90, 247, 0.15)';
            const borderColor = 'rgba(124, 90, 247, 0.8)';
            const pointColors = Object.values(ALPHA_META).map(m => m.color);

            const config = {
                type: 'radar',
                data: {
                    labels: labels,
                    datasets: [{
                        label: 'Systemic Heat',
                        data: dataVals,
                        backgroundColor: bgColor,
                        borderColor: borderColor,
                        borderWidth: 2,
                        pointBackgroundColor: pointColors,
                        pointBorderColor: '#fff',
                        pointHoverBackgroundColor: '#fff',
                        pointHoverBorderColor: pointColors,
                        pointRadius: 4,
                        pointHoverRadius: 6,
                        fill: true,
                        tension: 0.3
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: { duration: 400, easing: 'easeOutQuart' },
                    scales: {
                        r: {
                            angleLines: { color: 'rgba(140, 160, 200, 0.15)' },
                            grid: { color: 'rgba(140, 160, 200, 0.15)' },
                            pointLabels: {
                                color: 'rgba(140, 160, 200, 0.85)',
                                font: { family: "'Space Grotesk', sans-serif", size: 10, weight: '500' }
                            },
                            ticks: {
                                display: false,
                                min: 0,
                                max: 100,
                                stepSize: 20
                            }
                        }
                    },
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            backgroundColor: 'rgba(15, 20, 30, 0.9)',
                            titleColor: '#fff',
                            bodyColor: '#fff',
                            titleFont: { family: "'JetBrains Mono', monospace", size: 12 },
                            bodyFont: { family: "'JetBrains Mono', monospace", size: 11 },
                            borderColor: 'rgba(140, 160, 200, 0.2)',
                            borderWidth: 1,
                            displayColors: false,
                            callbacks: {
                                label: function(ctx) {
                                    return 'Intensity: ' + ctx.raw.toFixed(1) + '%';
                                }
                            }
                        }
                    }
                }
            };

            this.radarChart = new Chart(this.canvasEl, config);
        }

        updateData(data) {
            if (!this.radarChart || !data || !data.signals) return;
            
            const signals = data.signals;
            const normalizedValues = [];

            Object.keys(ALPHA_META).forEach(key => {
                const meta = ALPHA_META[key];
                const raw = signals[key];
                let fill = 0;

                if (raw != null && typeof raw === 'object') {
                    const v = raw.value ?? raw.signal ?? raw.score ?? raw.magnetization ?? raw.entropy ?? raw.reynolds ?? null;
                    if (v != null) {
                        fill = Math.min(100, Math.abs(v) / meta.hi * 100);
                    }
                } else if (raw != null) {
                    fill = Math.min(100, Math.abs(parseFloat(raw) || 0) / meta.hi * 100);
                }
                normalizedValues.push(fill);
            });

            this.radarChart.data.datasets[0].data = normalizedValues;
            
            // Dynamically shift background color based on high risk
            const avg = normalizedValues.reduce((a,b)=>a+b,0) / normalizedValues.length;
            if (avg > 70) {
                this.radarChart.data.datasets[0].backgroundColor = 'rgba(232, 67, 90, 0.25)';
                this.radarChart.data.datasets[0].borderColor = 'rgba(232, 67, 90, 0.9)';
            } else {
                this.radarChart.data.datasets[0].backgroundColor = 'rgba(124, 90, 247, 0.15)'; 
                this.radarChart.data.datasets[0].borderColor = 'rgba(124, 90, 247, 0.8)';
            }

            this.radarChart.update();
        }

        destroy() {
            if (this.radarChart) {
                this.radarChart.destroy();
                this.radarChart = null;
            }
            if (window.AltarisEvents) {
                window.AltarisEvents.off('data:zone:update', this.updateHandler);
            }
            if (this.canvasEl) {
                this.canvasEl.remove();
            }
            this.container.innerHTML = '';
        }
    }

    // Module Expose
    window.AlphaDashboard = {
        init: function(container) {
            // Cleanup existing if there's one mounted in this container somehow
            if (instances.has(container)) {
                instances.get(container).destroy();
                instances.delete(container);
            }
            const instance = new AlphaDashboardInstance(container);
            instances.set(container, instance);
            return instance;
        },
        destroy: function(container) {
            // Note: Since layout engine provides slotEl directly on unmount, 
            // we should refactor app.js to pass the container when destroying.
        },
        destroyInstance: function(container) {
            if (instances.has(container)) {
                instances.get(container).destroy();
                instances.delete(container);
            }
        }
    };

})();
