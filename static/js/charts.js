document.addEventListener('DOMContentLoaded', async () => {
    try {
        const response = await fetch('/api/dashboard-data');
        if (!response.ok) throw new Error('Network response was not ok');
        const data = await response.json();

        Chart.defaults.color = '#94a3b8';
        Chart.defaults.font.family = "'Inter', sans-serif";

        const gridLinesColor = 'rgba(255, 255, 255, 0.05)';
        const moneyTick = (value) => `Rs ${value}`;
        const formatMoney = (value) => `Rs ${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
        const safePercent = (value, total) => total > 0 ? ((value / total) * 100) : 0;
        const setMeta = (id, lines) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.innerHTML = lines.map((line) => `<div>${line}</div>`).join('');
        };

        const percentageTooltip = (context, total) => {
            const value = context.raw ?? 0;
            const percent = safePercent(value, total).toFixed(1);
            return `${context.label}: ${formatMoney(value)} (${percent}%)`;
        };

        let pulseChart;
        const pulseSeries = data.pulse.series;
        const renderPulseMeta = (period) => {
            const labels = pulseSeries[period].labels;
            const values = pulseSeries[period].data;
            const latest = values.length ? values[values.length - 1] : 0;
            setMeta('pulseMeta', [
                `Current approved group fund: ${formatMoney(data.pulse.total || 0)}`,
                `${period[0].toUpperCase() + period.slice(1)} view latest cumulative point: ${formatMoney(latest)}`,
                `Showing ${labels.length} ${period} data point(s).`
            ]);
        };

        const ctxPulse = document.getElementById('pulseChart');
        if (ctxPulse) {
            const initialPeriod = 'daily';
            pulseChart = new Chart(ctxPulse, {
                type: 'line',
                data: {
                    labels: pulseSeries[initialPeriod].labels,
                    datasets: [{
                        label: 'Total Fund Growth',
                        data: pulseSeries[initialPeriod].data,
                        borderColor: '#2dd4bf',
                        backgroundColor: 'rgba(45, 212, 191, 0.1)',
                        borderWidth: 2,
                        fill: true,
                        tension: 0.4
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: (context) => {
                                    const value = context.raw ?? 0;
                                    const percent = safePercent(value, data.pulse.total || 0).toFixed(1);
                                    return `${formatMoney(value)} (${percent}% of current fund)`;
                                }
                            }
                        }
                    },
                    scales: {
                        x: { grid: { color: gridLinesColor } },
                        y: { grid: { color: gridLinesColor }, ticks: { callback: moneyTick } }
                    }
                }
            });
            renderPulseMeta(initialPeriod);

            document.querySelectorAll('.pulse-toggle').forEach((button) => {
                button.addEventListener('click', () => {
                    const period = button.dataset.period;
                    pulseChart.data.labels = pulseSeries[period].labels;
                    pulseChart.data.datasets[0].data = pulseSeries[period].data;
                    pulseChart.update();
                    document.querySelectorAll('.pulse-toggle').forEach((btn) => btn.classList.remove('is-active'));
                    button.classList.add('is-active');
                    renderPulseMeta(period);
                });
            });
        }

        const ctxEquity = document.getElementById('equityChart');
        if (ctxEquity) {
            const used = data.equity.used;
            const available = data.equity.available;
            const ceiling = data.equity.ceiling;
            new Chart(ctxEquity, {
                type: 'doughnut',
                data: {
                    labels: ['Used Capacity', 'Available Capacity'],
                    datasets: [{
                        data: [used, available],
                        backgroundColor: ['#fb7185', '#6366f1'],
                        borderWidth: 0,
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    circumference: 180,
                    rotation: 270,
                    cutout: '80%',
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                label: (context) => percentageTooltip(context, ceiling)
                            }
                        }
                    }
                }
            });

            setMeta('equityMeta', [
                `Used: ${safePercent(used, ceiling).toFixed(1)}% (${formatMoney(used)})`,
                `Available: ${safePercent(available, ceiling).toFixed(1)}% (${formatMoney(available)})`
            ]);
        }

        const ctxHealth = document.getElementById('healthChart');
        if (ctxHealth) {
            const totalHealth = data.health.liquid + data.health.loans;
            new Chart(ctxHealth, {
                type: 'doughnut',
                data: {
                    labels: ['Liquid Cash', 'Active Loans'],
                    datasets: [{
                        data: [data.health.liquid, data.health.loans],
                        backgroundColor: ['#10b981', '#ef4444'],
                        borderWidth: 0,
                        hoverOffset: 4
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: '70%',
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                label: (context) => percentageTooltip(context, totalHealth)
                            }
                        }
                    }
                }
            });

            setMeta('healthMeta', [
                `Liquid cash: ${safePercent(data.health.liquid, totalHealth).toFixed(1)}%`,
                `Active loans: ${safePercent(data.health.loans, totalHealth).toFixed(1)}%`
            ]);
        }

        const ctxBaseline = document.getElementById('baselineChart');
        if (ctxBaseline) {
            const target = data.baseline.data[0] || 0;
            const actual = data.baseline.data[1] || 0;
            new Chart(ctxBaseline, {
                type: 'bar',
                data: {
                    labels: data.baseline.labels,
                    datasets: [{
                        data: data.baseline.data,
                        backgroundColor: ['#818cf8', '#5eead4'],
                        borderRadius: 12,
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: {
                            callbacks: {
                                label: (context) => {
                                    const value = context.raw ?? 0;
                                    const percent = safePercent(value, target).toFixed(1);
                                    return `${formatMoney(value)} (${percent}% of target)`;
                                }
                            }
                        }
                    },
                    scales: {
                        x: { grid: { display: false } },
                        y: { grid: { color: gridLinesColor }, ticks: { callback: moneyTick } }
                    }
                }
            });

            setMeta('baselineMeta', [
                `Achievement: ${data.baseline.achievementPercent.toFixed(1)}% of target`,
                `Gap to target: ${formatMoney(Math.max(0, target - actual))}`
            ]);
        }

        const ctxSplit = document.getElementById('splitChart');
        if (ctxSplit) {
            const splitTotal = data.personalSplit.data.reduce((sum, value) => sum + value, 0);
            new Chart(ctxSplit, {
                type: 'pie',
                data: {
                    labels: data.personalSplit.labels,
                    datasets: [{
                        data: data.personalSplit.data,
                        backgroundColor: ['#5eead4', '#fb7185'],
                        borderWidth: 0,
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'bottom' },
                        tooltip: {
                            callbacks: {
                                label: (context) => percentageTooltip(context, splitTotal)
                            }
                        }
                    }
                }
            });

            setMeta('splitMeta', [
                `${data.personalSplit.labels[0]} share: ${safePercent(data.personalSplit.data[0], splitTotal).toFixed(1)}%`,
                `${data.personalSplit.labels[1]} share: ${safePercent(data.personalSplit.data[1], splitTotal).toFixed(1)}%`
            ]);
        }
    } catch (error) {
        console.error('Error loading chart data:', error);
    }
});
