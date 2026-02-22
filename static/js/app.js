const socket = io();

let currentConfig = null;
const configModal = new bootstrap.Modal(document.getElementById('configModal'));
let isBotRunning = false;
let activeTrades = [];

document.addEventListener('DOMContentLoaded', () => {
    loadConfig();
    setupEventListeners();
    setupSocketListeners();
    startCountdownTimer();
});

function updateConfigLabels() {
    // Entry Type Label
    const strategySelect = document.getElementById('configActiveStrategy');
    if (strategySelect) {
        const strategy = strategySelect.value;
        const label = document.getElementById('configEntryTypeLabel');
        const customExpiryContainer = document.getElementById('customExpiryContainer');
        const strategy5Options = document.getElementById('strategy5Options');
        const screenerTabNavItem = document.getElementById('screenerTabNavItem');

        // Hide screener tab by default
        if (screenerTabNavItem) screenerTabNavItem.style.display = 'none';

        if (strategy === 'strategy_1') {
            label.textContent = "Wait for 15m Candle Close";
            customExpiryContainer.style.display = 'none';
            strategy5Options.style.display = 'none';
        } else if (strategy === 'strategy_2') {
            label.textContent = "Wait for 3m Candle Close";
            customExpiryContainer.style.display = 'block';
            strategy5Options.style.display = 'none';
        } else if (strategy === 'strategy_4') {
            label.textContent = "Wait for 1m Candle Close";
            customExpiryContainer.style.display = 'block';
            strategy5Options.style.display = 'none';
        } else if (strategy === 'strategy_5') {
            label.textContent = "Wait for 1m Candle Close";
            customExpiryContainer.style.display = 'none'; // Strategy 5 uses dynamic expiry
            strategy5Options.style.display = 'block';
            document.getElementById('screenerTabNavItem').style.display = 'block';
        } else {
            document.getElementById('screenerTabNavItem').style.display = 'none';
            label.textContent = "Wait for 1m Candle Close";
            customExpiryContainer.style.display = 'block';
            strategy5Options.style.display = 'none';
        }
    }

    // TP/SL Unit Labels
    const useFixed = document.getElementById('configUseFixedBalance').checked;
    const tpLabel = document.getElementById('configTpLabel');
    const slLabel = document.getElementById('configSlLabel');
    if (useFixed) {
        tpLabel.textContent = "Take Profit ($)";
        slLabel.textContent = "Stop Loss ($)";
    } else {
        tpLabel.textContent = "Take Profit (%)";
        slLabel.textContent = "Stop Loss (%)";
    }
}

function setupEventListeners() {
    document.getElementById('configActiveStrategy').addEventListener('change', updateConfigLabels);
    document.getElementById('configContractType').addEventListener('change', () => {
        updateConfigLabels();
        if (currentConfig) {
            currentConfig.contract_type = document.getElementById('configContractType').value;
            // Refresh screener table if data exists
            updateScreenerTable(null, null);
        }
    });
    document.getElementById('configUseFixedBalance').addEventListener('change', updateConfigLabels);
    document.getElementById('themeToggle').addEventListener('change', (e) => {
        document.body.setAttribute('data-theme', e.target.checked ? 'light' : 'dark');
    });

    document.getElementById('startStopBtn').addEventListener('click', () => {
        if (isBotRunning) {
            socket.emit('stop_bot');
        } else {
            socket.emit('start_bot');
        }
    });

    document.getElementById('configBtn').addEventListener('click', () => {
        if (currentConfig) {
            document.getElementById('configApiToken').value = currentConfig.deriv_api_token || '';
            document.getElementById('configAppId').value = currentConfig.deriv_app_id || '62845';
            document.getElementById('configUseFixedBalance').checked = currentConfig.use_fixed_balance !== false;
            document.getElementById('configBalanceValue').value = currentConfig.balance_value || 10;
            document.getElementById('configMaxDailyLoss').value = currentConfig.max_daily_loss_pct || 5;
            document.getElementById('configTpEnabled').checked = currentConfig.tp_enabled || false;
            document.getElementById('configTpValue').value = currentConfig.tp_value || 0;
            document.getElementById('configSlEnabled').checked = currentConfig.sl_enabled || false;
            document.getElementById('configSlValue').value = currentConfig.sl_value || 0;
            document.getElementById('configForceCloseEnabled').checked = currentConfig.force_close_enabled || false;
            document.getElementById('configForceCloseDuration').value = currentConfig.force_close_duration || 60;
            document.getElementById('configActiveStrategy').value = currentConfig.active_strategy || 'strategy_1';
            document.getElementById('configContractType').value = currentConfig.contract_type || 'rise_fall';
            document.getElementById('configCustomExpiry').value = currentConfig.custom_expiry || 'default';
            document.getElementById('configEntryType').value = currentConfig.entry_type || 'candle_close';
            document.getElementById('configIsDemo').checked = currentConfig.is_demo !== false;
            updateConfigLabels();
        }
        configModal.show();
    });

    document.getElementById('saveConfigBtn').addEventListener('click', saveConfig);

    document.getElementById('clearConsoleBtn').addEventListener('click', () => {
        document.getElementById('consoleOutput').innerHTML = '';
    });

    document.getElementById('downloadLogsBtn').addEventListener('click', () => {
        window.location.href = '/api/download_logs';
    });

    document.getElementById('addSymbolBtn').addEventListener('click', () => {
        const symbol = prompt("Enter symbol name (e.g., R_100, frxEURUSD):");
        if (symbol && currentConfig) {
            if (!currentConfig.symbols.includes(symbol)) {
                currentConfig.symbols.push(symbol);
                updateSymbolList();
                saveLiveConfig();
            }
        }
    });
}

function setupSocketListeners() {
    socket.on('bot_status', (data) => {
        isBotRunning = data.running;
        const btn = document.getElementById('startStopBtn');
        const status = document.getElementById('botStatus');
        if (isBotRunning) {
            btn.innerHTML = '<i class="bi bi-stop-fill"></i> <span>Stop</span>';
            btn.className = 'btn btn-danger';
            status.textContent = 'Running';
            status.className = 'badge rounded-pill status-badge bg-success mb-3';
        } else {
            btn.innerHTML = '<i class="bi bi-play-fill"></i> <span>Start</span>';
            btn.className = 'btn btn-primary';
            status.textContent = 'Stopped';
            status.className = 'badge rounded-pill status-badge bg-secondary mb-3';
        }
    });

    socket.on('account_update', (data) => {
        const typeBadge = document.getElementById('accountTypeBadge');
        if (data.is_demo) {
            typeBadge.textContent = 'Demo';
            typeBadge.className = 'badge rounded-pill bg-info ms-1';
        } else {
            typeBadge.textContent = 'Live';
            typeBadge.className = 'badge rounded-pill bg-danger ms-1';
        }

        document.getElementById('balanceDisplay').textContent = `$${Number(data.total_balance || 0).toFixed(2)}`;
        document.getElementById('totalPnlDisplay').textContent = `$${Number(data.net_profit || 0).toFixed(2)}`;
        document.getElementById('totalPnlDisplay').className = `stat-value ${data.net_profit >= 0 ? 'text-success' : 'text-danger'}`;

        document.getElementById('tradesCountDisplay').textContent = data.total_trades || 0;
        document.getElementById('usedAmountDisplay').textContent = `$${Number(data.used_amount || 0).toFixed(2)}`;
        document.getElementById('realizedPnlDisplay').textContent = `$${Number(data.net_trade_profit || 0).toFixed(2)}`;
        document.getElementById('floatingPnlDisplay').textContent = `$${Number((data.net_profit || 0) - (data.net_trade_profit || 0)).toFixed(2)}`;

        if (document.getElementById('winRateDisplay')) {
            document.getElementById('winRateDisplay').textContent = `${data.win_rate || 0}%`;
        }
        if (document.getElementById('avgPnlDisplay')) {
            const avg = data.avg_pnl || 0;
            const el = document.getElementById('avgPnlDisplay');
            el.textContent = `$${Number(avg).toFixed(2)}`;
            el.className = `stat-value ${avg >= 0 ? 'text-success' : 'text-danger'}`;
        }
    });

    socket.on('trades_update', (data) => {
        activeTrades = data.trades;
        updateActiveTrades(data.trades);
    });

    socket.on('screener_update', (data) => {
        updateScreenerTable(data.symbol, data.data);
    });

    socket.on('console_log', (data) => {
        const consoleOutput = document.getElementById('consoleOutput');
        const line = document.createElement('div');
        line.style.marginBottom = '2px';
        line.innerHTML = `<span class="text-muted small">[${data.timestamp}]</span> <span class="${data.level === 'error' ? 'text-danger' : (data.level === 'warning' ? 'text-warning' : 'text-success')}">${data.message}</span>`;
        consoleOutput.appendChild(line);
        consoleOutput.scrollTop = consoleOutput.scrollHeight;
    });

    socket.on('error', (data) => alert('Error: ' + data.message));
    socket.on('success', (data) => console.log('Success:', data.message));

    socket.on('multipliers_update', (data) => {
        const symbol = data.symbol;
        const multipliers = data.multipliers;
        window.symbolMultipliers = window.symbolMultipliers || {};
        window.symbolMultipliers[symbol] = multipliers;
    });
}

const screenerDataMap = {};

function updateScreenerTable(symbol, data) {
    if (symbol && data) {
        screenerDataMap[symbol] = data;
    }
    const body = document.getElementById('screenerTableBody');
    if (!body) return;

    body.innerHTML = Object.keys(screenerDataMap).sort().map(sym => {
        const d = screenerDataMap[sym];
        const confColor = d.confidence >= 65 ? 'text-success' : (d.confidence <= -65 ? 'text-danger' : 'text-warning');
        const dirColor = d.direction === 'CALL' ? 'text-success' : 'text-danger';

        const contractType = currentConfig ? currentConfig.contract_type : 'rise_fall';
        let recommendation = "";
        if (contractType === 'multiplier') {
            recommendation = `x${d.multiplier} | ATR:${d.atr}`;
        } else {
            recommendation = `${d.expiry_min}m | 1mATR:${d.atr_1m}`;
        }

        return `
            <tr>
                <td><strong>${sym}</strong></td>
                <td class="${confColor} fw-bold">${d.confidence}%</td>
                <td class="${dirColor} fw-bold">${d.direction}</td>
                <td><small>${recommendation}</small></td>
                <td>${d.trend}</td>
                <td>${d.momentum}</td>
                <td>${d.volatility}</td>
                <td>${d.structure}</td>
            </tr>
        `;
    }).join('');
}

function updateActiveTrades(trades) {
    const container = document.getElementById('activeTradesContainer');
    if (!trades || !Array.isArray(trades) || trades.length === 0) {
        container.innerHTML = '<p class="text-muted text-center py-4">No active positions</p>';
        return;
    }

    container.innerHTML = trades.map(t => {
        const pnl = typeof t.pnl === 'number' ? t.pnl : 0;
        const entry = typeof t.entry_spot_price === 'number' ? t.entry_spot_price : 0;
        const stake = typeof t.stake === 'number' ? t.stake : 0;
        const typeLabel = t.type ? t.type.toLowerCase() : 'unknown';

        const statusLabel = t.status === 'Active' ? '' : ` [${t.status}]`;
        const freerideLabel = t.is_freeride ? ' <span class="badge bg-success">FREE RIDE</span>' : '';

        return `
            <div class="trade-card ${typeLabel}">
                <div class="d-flex justify-content-between align-items-center">
                    <strong>${t.symbol || 'Unknown'} (${t.type || '???'})${statusLabel}${freerideLabel}</strong>
                    <div class="d-flex align-items-center gap-3">
                        <span class="${pnl >= 0 ? 'text-success' : 'text-danger'} fw-bold">$${pnl.toFixed(2)}</span>
                        <button class="btn btn-sm btn-outline-danger" onclick="closeTrade('${t.id}')" title="Close Trade">
                            <i class="bi bi-x-circle"></i>
                        </button>
                    </div>
                </div>
                <div class="small text-muted d-flex justify-content-between mt-1">
                    <div>ID: ${t.id} | Entry: ${entry.toFixed(4)} | Stake: $${stake.toFixed(2)}</div>
                    <div class="expiry-countdown text-warning" data-expiry="${t.expiry_time}">${formatCountdown(t.expiry_time)}</div>
                </div>
            </div>
        `;
    }).join('');
}

function closeTrade(id) {
    if (confirm(`Are you sure you want to close trade ${id}?`)) {
        socket.emit('close_trade', { contract_id: id });
    }
}

function startCountdownTimer() {
    setInterval(() => {
        document.querySelectorAll('.expiry-countdown').forEach(el => {
            const expiry = parseInt(el.getAttribute('data-expiry'));
            el.textContent = formatCountdown(expiry);
        });
    }, 1000);
}

function formatCountdown(expiryEpoch) {
    if (!expiryEpoch) return "";
    const now = Math.floor(Date.now() / 1000);
    let diff = expiryEpoch - now;
    if (diff <= 0) return "Expired";

    const h = Math.floor(diff / 3600);
    const m = Math.floor((diff % 3600) / 60);
    const s = diff % 60;

    return [h, m, s].map(v => v.toString().padStart(2, '0')).join(':');
}

async function loadConfig() {
    const res = await fetch('/api/config');
    currentConfig = await res.json();
    updateSymbolList();
}

function updateSymbolList() {
    const list = document.getElementById('symbolList');
    list.innerHTML = currentConfig.symbols.map(s => `
        <li class="list-group-item d-flex justify-content-between align-items-center bg-transparent border-secondary text-light">
            ${s}
            <i class="bi bi-trash text-danger cursor-pointer" onclick="removeSymbol('${s}')" style="cursor: pointer;"></i>
        </li>
    `).join('');
}

function removeSymbol(symbol) {
    currentConfig.symbols = currentConfig.symbols.filter(s => s !== symbol);
    updateSymbolList();
    saveLiveConfig();
}

async function saveConfig() {
    const config = {
        deriv_api_token: document.getElementById('configApiToken').value,
        deriv_app_id: document.getElementById('configAppId').value,
        use_fixed_balance: document.getElementById('configUseFixedBalance').checked,
        balance_value: parseFloat(document.getElementById('configBalanceValue').value),
        max_daily_loss_pct: parseFloat(document.getElementById('configMaxDailyLoss').value),
        tp_enabled: document.getElementById('configTpEnabled').checked,
        tp_value: parseFloat(document.getElementById('configTpValue').value),
        sl_enabled: document.getElementById('configSlEnabled').checked,
        sl_value: parseFloat(document.getElementById('configSlValue').value),
        force_close_enabled: document.getElementById('configForceCloseEnabled').checked,
        force_close_duration: parseInt(document.getElementById('configForceCloseDuration').value),
        active_strategy: document.getElementById('configActiveStrategy').value,
        contract_type: document.getElementById('configContractType').value,
        custom_expiry: document.getElementById('configCustomExpiry').value,
        entry_type: document.getElementById('configEntryType').value,
        is_demo: document.getElementById('configIsDemo').checked,
        symbols: currentConfig.symbols
    };

    const res = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config)
    });

    if (res.ok) {
        currentConfig = config;
        configModal.hide();
    }
}

async function saveLiveConfig() {
    await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(currentConfig)
    });
}
