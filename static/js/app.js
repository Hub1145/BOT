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

function updateEntryTypeLabel() {
    const strategySelect = document.getElementById('configActiveStrategy');
    if (!strategySelect) return;
    const strategy = strategySelect.value;
    const label = document.getElementById('configEntryTypeLabel');
    if (strategy === 'strategy_1') {
        label.textContent = "Wait for 15m Candle Close";
    } else {
        label.textContent = "Wait for 3m Candle Close";
    }
}

function setupEventListeners() {
    document.getElementById('configActiveStrategy').addEventListener('change', updateEntryTypeLabel);
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
            document.getElementById('configBalanceType').value = currentConfig.use_fixed_balance ? 'fixed' : 'percent';
            document.getElementById('configBalanceValue').value = currentConfig.balance_value || 10;
            document.getElementById('configMaxDailyLoss').value = currentConfig.max_daily_loss_pct || 5;
            document.getElementById('configTpEnabled').checked = currentConfig.tp_enabled || false;
            document.getElementById('configTpValue').value = currentConfig.tp_value || 0;
            document.getElementById('configSlEnabled').checked = currentConfig.sl_enabled || false;
            document.getElementById('configSlValue').value = currentConfig.sl_value || 0;
            document.getElementById('configActiveStrategy').value = currentConfig.active_strategy || 'strategy_1';
            document.getElementById('configEntryType').value = currentConfig.entry_type || 'candle_close';
            document.getElementById('configIsDemo').checked = currentConfig.is_demo !== false;
            updateEntryTypeLabel();
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
    });

    socket.on('trades_update', (data) => {
        activeTrades = data.trades;
        updateActiveTrades(data.trades);
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
}

function updateActiveTrades(trades) {
    const container = document.getElementById('activeTradesContainer');
    if (!trades || trades.length === 0) {
        container.innerHTML = '<p class="text-muted text-center py-4">No active positions</p>';
        return;
    }

    container.innerHTML = trades.map(t => `
        <div class="trade-card ${t.type.toLowerCase()}">
            <div class="d-flex justify-content-between">
                <strong>${t.symbol} (${t.type})</strong>
                <span class="${t.pnl >= 0 ? 'text-success' : 'text-danger'} font-weight-bold">$${t.pnl.toFixed(2)}</span>
            </div>
            <div class="small text-muted d-flex justify-content-between">
                <div>ID: ${t.id} | Entry: ${t.entry_spot_price.toFixed(4)} | Stake: $${t.stake.toFixed(2)}</div>
                <div class="expiry-countdown text-warning" data-expiry="${t.expiry_time}">${formatCountdown(t.expiry_time)}</div>
            </div>
        </div>
    `).join('');
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
        use_fixed_balance: document.getElementById('configBalanceType').value === 'fixed',
        balance_value: parseFloat(document.getElementById('configBalanceValue').value),
        max_daily_loss_pct: parseFloat(document.getElementById('configMaxDailyLoss').value),
        tp_enabled: document.getElementById('configTpEnabled').checked,
        tp_value: parseFloat(document.getElementById('configTpValue').value),
        sl_enabled: document.getElementById('configSlEnabled').checked,
        sl_value: parseFloat(document.getElementById('configSlValue').value),
        active_strategy: document.getElementById('configActiveStrategy').value,
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
