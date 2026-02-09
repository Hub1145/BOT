const socket = io();

let currentConfig = null;
const configModal = new bootstrap.Modal(document.getElementById('configModal'));
let isBotRunning = false;
let isTransitioning = false;
let orderExpirationCache = {}; // Cache to store calcualted expiration timestamps
let lastUsedFee = 0; // Track last known used fee for Auto-Cal calculation
let lastSizeFee = 0; // Track last known Size Fee for Auto-Cal Size calculation

document.addEventListener('DOMContentLoaded', () => {
    initializeTheme();
    loadConfig().then(() => {
        setupEventListeners();
        setupSocketListeners();
        loadStatus(); // Load initial status after listeners are set up
        startUITimers(); // Start local countdown ticks
    });
});

function initializeTheme() {
    const savedTheme = localStorage.getItem('theme') || 'dark';
    document.body.setAttribute('data-theme', savedTheme);
    document.getElementById('themeToggle').checked = savedTheme === 'light';
    updateThemeIcon(savedTheme);
}

function updateThemeIcon(theme) {
    const icon = document.getElementById('themeIcon');
    icon.className = theme === 'light' ? 'bi bi-sun-fill' : 'bi bi-moon-stars';
}

function setupEventListeners() {
    document.getElementById('themeToggle').addEventListener('change', (e) => {
        const theme = e.target.checked ? 'light' : 'dark';
        document.body.setAttribute('data-theme', theme);
        localStorage.setItem('theme', theme);
        updateThemeIcon(theme);
    });

    document.getElementById('startStopBtn').addEventListener('click', () => {
        const btn = document.getElementById('startStopBtn');
        btn.disabled = true;
        isTransitioning = true; // Mark as transitioning to block early status syncs

        if (isBotRunning) {
            // Optimistic Update: Stopping
            btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Stopping...';
            btn.className = 'btn btn-warning w-100'; // Transition color
            socket.emit('stop_bot');
        } else {
            // Optimistic Update: Starting
            btn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Starting...';
            btn.className = 'btn btn-info w-100'; // Transition color
            socket.emit('start_bot');
        }

        // Safety timeout to re-enable button if response is lost
        setTimeout(() => {
            if (isTransitioning) {
                console.warn('Start/Stop timeout - re-enabling button');
                isTransitioning = false;
                btn.disabled = false;
                loadStatus(); // Force state sync
            }
        }, 8000); // Increased to 8s to account for slow initialization
    });

    document.getElementById('configBtn').addEventListener('click', () => {
        loadConfigToModal();
        configModal.show();
    });

    document.getElementById('saveConfigBtn').addEventListener('click', () => {
        saveConfig();
    });

    document.getElementById('clearConsoleBtn').addEventListener('click', () => {
        socket.emit('clear_console');
        document.getElementById('consoleOutput').innerHTML = '<p class="text-muted">Console cleared</p>';
    });

    document.getElementById('downloadLogsBtn').addEventListener('click', () => {
        window.location.href = '/api/download_logs';
    });

    // Event listener for Emergency SL button
    document.getElementById('emergencySlBtn').addEventListener('click', () => {
        if (confirm('Are you sure you want to trigger an emergency Stop Loss? This will close all open positions at market price.')) {
            socket.emit('emergency_sl');
        }
    });

    // Event listener for Batch Modify TP/SL button
    document.getElementById('batchModifyTPSLBtn').addEventListener('click', () => {
        if (confirm('Are you sure you want to batch modify TP/SL for all open orders?')) {
            socket.emit('batch_modify_tpsl');
        }
    });

    // Event listener for Batch Cancel Orders button
    document.getElementById('batchCancelOrdersBtn').addEventListener('click', () => {
        if (confirm('Are you sure you want to batch cancel all open orders?')) {
            socket.emit('batch_cancel_orders');
        }
    });

    // Manual Fee Refresh
    const refreshFeesBtn = document.getElementById('refreshFeesBtn');
    if (refreshFeesBtn) {
        refreshFeesBtn.addEventListener('click', () => {
            loadConfig();
            loadStatus();
        });
    }

    // Refresh trade metric on fee percentage change
    const feeInput = document.getElementById('tradeFeePercentage');
    if (feeInput) {
        feeInput.addEventListener('change', () => {
            if (currentConfig) {
                currentConfig.trade_fee_percentage = parseFloat(feeInput.value);
            }
        });
    }

    // Event listener for useCandlestickConditions checkbox
    document.getElementById('useCandlestickConditions').addEventListener('change', toggleCandlestickInputs);
    // Call on load to set initial state
    toggleCandlestickInputs();

    // PnL Auto-Cancel listeners (New Dual Mode)
    document.getElementById('usePnlAutoManual').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Manual Profit: ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('pnlAutoManualThreshold').addEventListener('change', saveLiveConfigs);

    document.getElementById('usePnlAutoCal').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Cal Profit: ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('pnlAutoCalTimes').addEventListener('input', () => {
        updateAutoCalDisplay();
    });
    document.getElementById('pnlAutoCalTimes').addEventListener('change', saveLiveConfigs);

    // Auto-Cal Loss listeners
    document.getElementById('usePnlAutoCalLoss').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Cal Loss (Close All): ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('pnlAutoCalLossTimes').addEventListener('input', () => {
        updateAutoCalDisplay();
    });
    document.getElementById('pnlAutoCalLossTimes').addEventListener('change', saveLiveConfigs);

    // Auto-Cal Size (Profit) listeners
    document.getElementById('useSizeAutoCal').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Cal Size (Profit): ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('sizeAutoCalTimes').addEventListener('input', () => {
        updateAutoCalDisplay();
    });
    document.getElementById('sizeAutoCalTimes').addEventListener('change', saveLiveConfigs);

    // Auto-Cal Size Loss listeners
    document.getElementById('useSizeAutoCalLoss').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Cal Size Loss: ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('sizeAutoCalLossTimes').addEventListener('input', () => {
        updateAutoCalDisplay();
    });
    document.getElementById('sizeAutoCalLossTimes').addEventListener('change', saveLiveConfigs);

    // Auto-Add Margin listeners
    document.getElementById('useAutoMargin').addEventListener('change', (e) => {
        const state = e.target.checked ? 'ACTIVATED' : 'DEACTIVATED';
        addConsoleLog({ message: `Auto-Add Margin: ${state}`, level: 'info' });
        saveLiveConfigs();
    });
    document.getElementById('autoMarginOffset').addEventListener('change', saveLiveConfigs);

    document.getElementById('tradeFeePercentage').addEventListener('change', saveLiveConfigs);

    // Sync Dashboard Card with Modal Inputs
    document.querySelectorAll('.dashboard-sync').forEach(el => {
        el.addEventListener('change', (e) => {
            const syncId = e.target.dataset.syncId;
            const modalInput = document.getElementById(syncId);
            if (modalInput) {
                if (e.target.type === 'checkbox') {
                    modalInput.checked = e.target.checked;
                } else {
                    modalInput.value = e.target.value;
                }
                // Trigger change on modal input to invoke saveLiveConfigs
                modalInput.dispatchEvent(new Event('change'));
            }
        });
    });

    document.getElementById('testApiKeyBtn').addEventListener('click', testApiKey);
}

function toggleCandlestickInputs() {
    const isChecked = document.getElementById('useCandlestickConditions').checked;
    const elementsToToggle = [
        document.getElementById('candlestickTimeframe'),
        document.getElementById('useChgOpenClose'),
        document.getElementById('minChgOpenClose'),
        document.getElementById('maxChgOpenClose'),
        document.getElementById('useChgHighLow'),
        document.getElementById('minChgHighLow'),
        document.getElementById('maxChgHighLow'),
        document.getElementById('useChgHighClose'),
        document.getElementById('minChgHighClose'),
        document.getElementById('maxChgHighClose'),
    ];

    elementsToToggle.forEach(element => {
        element.disabled = !isChecked;
        // Also ensure checkboxes are unchecked if the main toggle is off
        if (element.type === 'checkbox' && !isChecked) {
            element.checked = false;
        }
        // Also clear numeric inputs if disabled
        if (element.type === 'number' && !isChecked) {
            element.value = 0;
        }
    });
}

async function testApiKey() {
    const testBtn = document.getElementById('testApiKeyBtn');
    const originalBtnHtml = testBtn.innerHTML; // Store original button content
    testBtn.disabled = true;
    testBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Testing...'; // Show loading spinner

    const useTestnet = document.getElementById('useTestnet').checked;
    const useDev = document.getElementById('useDeveloperApi').checked;
    let apiKey, apiSecret, passphrase;

    if (useDev) {
        if (useTestnet) {
            apiKey = document.getElementById('devDemoApiKey').value;
            apiSecret = document.getElementById('devDemoApiSecret').value;
            passphrase = document.getElementById('devDemoApiPassphrase').value;
        } else {
            apiKey = document.getElementById('devApiKey').value;
            apiSecret = document.getElementById('devApiSecret').value;
            passphrase = document.getElementById('devPassphrase').value;
        }
    } else {
        if (useTestnet) {
            apiKey = document.getElementById('okxDemoApiKey').value;
            apiSecret = document.getElementById('okxDemoApiSecret').value;
            passphrase = document.getElementById('okxDemoApiPassphrase').value;
        } else {
            apiKey = document.getElementById('okxApiKey').value;
            apiSecret = document.getElementById('okxApiSecret').value;
            passphrase = document.getElementById('okxPassphrase').value;
        }
    }

    try {
        const response = await fetch('/api/test_api_key', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                api_key: apiKey,
                api_secret: apiSecret,
                passphrase: passphrase,
                use_testnet: useTestnet
            }),
        });

        const result = await response.json();

        if (result.success) {
            showNotification('API Key test successful: ' + result.message, 'success');
        } else {
            showNotification('API Key test failed: ' + result.message, 'error');
        }
    } catch (error) {
        console.error('Error testing API key:', error);
        showNotification('Failed to connect to API test endpoint', 'error');
    } finally {
        testBtn.disabled = false;
        testBtn.innerHTML = originalBtnHtml; // Restore original button content
    }
}
function setupSocketListeners() {
    socket.on('connection_status', (data) => {
        console.log('Connected to server:', data);
    });

    socket.on('bot_status', (data) => {
        updateBotStatus(data.running);
    });

    socket.on('account_update', (data) => {
        updateAccountMetrics(data);
    });

    socket.on('trades_update', (data) => {
        updateOpenTrades(data.trades);
    });

    socket.on('position_update', (data) => {
        updatePositionDisplay(data);
    });

    socket.on('console_log', (data) => {
        addConsoleLog(data);
    });

    socket.on('console_cleared', () => {
        document.getElementById('consoleOutput').innerHTML = '<p class="text-muted">Console cleared</p>';
    });

    socket.on('price_update', (data) => {
    });

    socket.on('success', (data) => {
        showNotification(data.message, 'success');
    });

    socket.on('error', (data) => {
        showNotification(data.message, 'error');
        // Re-enable start/stop button if it was disabled during an attempt
        const btn = document.getElementById('startStopBtn');
        if (btn) btn.disabled = false;
        isTransitioning = false; // Stop the transition block
        loadStatus(); // Re-sync to current real state
    });

    socket.on('connect', () => {
        console.log('WebSocket connected');
        // Clear console on reconnect to avoid duplicate history logs
        const consoleEl = document.getElementById('consoleOutput');
        if (consoleEl) consoleEl.innerHTML = '';
        loadStatus();
    });

    socket.on('disconnect', () => {
        console.log('WebSocket disconnected');
    });
}

function updateBotStatus(running) {
    if (isTransitioning) {
        if (running === isBotRunning) {
            return;
        }
        isTransitioning = false;
    }

    isBotRunning = running;
    const statusBadge = document.getElementById('botStatusBadge') || document.getElementById('botStatus');
    const startStopBtn = document.getElementById('startStopBtn');

    if (statusBadge) {
        if (running) {
            statusBadge.textContent = 'Running';
            statusBadge.className = 'badge status-badge running';
        } else {
            statusBadge.textContent = 'Stopped';
            statusBadge.className = 'badge status-badge stopped';
        }
    }

    if (startStopBtn) {
        if (running) {
            startStopBtn.className = 'btn btn-danger w-100';
            startStopBtn.innerHTML = '<i class="bi bi-stop-fill"></i> <span id="btnText">Stop</span>';
        } else {
            startStopBtn.className = 'btn btn-success w-100';
            startStopBtn.innerHTML = '<i class="bi bi-play-fill"></i> <span id="btnText">Start</span>';
        }
        startStopBtn.disabled = false;
    }
}

function updateAccountMetrics(data) {
    if (!data) return;

    try {
        const elTotalCapital = document.getElementById('totalCapital');
        if (elTotalCapital) elTotalCapital.textContent = `$${data.total_capital !== undefined ? Number(data.total_capital).toFixed(2) : '0.00'}`;

        const elTotalCapital2nd = document.getElementById('totalCapital2nd');
        if (elTotalCapital2nd) elTotalCapital2nd.textContent = `$${data.total_capital_2_nd !== undefined ? Number(data.total_capital_2_nd).toFixed(2) : (data.total_capital_2nd !== undefined ? Number(data.total_capital_2nd).toFixed(2) : '0.00')}`;

        const elMaxAllowedUsedDisplay = document.getElementById('maxAllowedUsedDisplay');
        if (elMaxAllowedUsedDisplay) elMaxAllowedUsedDisplay.textContent = `$${data.max_allowed_used_display !== undefined ? Number(data.max_allowed_used_display).toFixed(2) : '0.00'}`;

        const elMaxAmountDisplay = document.getElementById('maxAmountDisplay');
        if (elMaxAmountDisplay) elMaxAmountDisplay.textContent = `$${data.max_amount_display !== undefined ? Number(data.max_amount_display).toFixed(2) : '0.00'}`;

        const elUsedAmount = document.getElementById('usedAmount');
        if (elUsedAmount) elUsedAmount.textContent = `$${data.used_amount !== undefined ? Number(data.used_amount).toFixed(2) : '0.00'}`;

        const remaining = data.remaining_amount !== undefined ? Number(data.remaining_amount) : 0.00;
        const minOrder = currentConfig?.min_order_amount || 0;
        const remainingEl = document.getElementById('remainingAmount');
        if (remainingEl) {
            if (remaining < minOrder && minOrder > 0) {
                remainingEl.textContent = 'No remaining balance for trade';
                remainingEl.classList.add('text-danger', 'small');
                remainingEl.style.fontSize = '0.75rem';
            } else {
                remainingEl.textContent = `$${remaining.toFixed(2)}`;
                remainingEl.classList.remove('text-danger', 'small');
                remainingEl.style.fontSize = '';
            }
        }

        const elNeedAddProfitTargetDisplay = document.getElementById('needAddProfitTargetDisplay');
        if (elNeedAddProfitTargetDisplay) elNeedAddProfitTargetDisplay.textContent = `$${data.need_add_usdt !== undefined ? Number(data.need_add_usdt).toFixed(2) : '0.00'}`;

        const elNeedAddAboveZeroDisplay = document.getElementById('needAddAboveZeroDisplay');
        if (elNeedAddAboveZeroDisplay) elNeedAddAboveZeroDisplay.textContent = `$${data.need_add_above_zero !== undefined ? Number(data.need_add_above_zero).toFixed(2) : '0.00'}`;

        const elBalance = document.getElementById('balance');
        if (elBalance) elBalance.textContent = `$${data.total_balance !== undefined ? Number(data.total_balance).toFixed(2) : '0.00'}`;

        const headerEl = document.getElementById('autoAddPosHeader');
        if (headerEl) {
            let side = '';
            if (data.positions && data.positions.short && data.positions.short.in) {
                side = 'Short';
            } else if (data.positions && data.positions.long && data.positions.long.in) {
                side = 'Long';
            } else if (data.in_position && typeof data.in_position === 'object') {
                if (data.in_position.short) side = 'Short';
                else if (data.in_position.long) side = 'Long';
            }

            if (side) {
                headerEl.textContent = `Auto-Cal Add ${side} Position`;
            } else {
                const configDirection = currentConfig?.direction;
                if (configDirection === 'short') {
                    headerEl.textContent = 'Auto-Cal Add Short Position';
                } else if (configDirection === 'long') {
                    headerEl.textContent = 'Auto-Cal Add Long Position';
                } else {
                    headerEl.textContent = 'Auto-Cal Add Position';
                }
            }
        }

        const netProfitElement = document.getElementById('netProfit');
        if (netProfitElement) {
            const netProfitValue = data.net_profit !== undefined ? Number(data.net_profit) : 0.00;
            netProfitElement.textContent = `$${netProfitValue.toFixed(2)}`;
            if (netProfitValue > 0) {
                netProfitElement.classList.remove('text-danger');
                netProfitElement.classList.add('text-success');
            } else if (netProfitValue < 0) {
                netProfitElement.classList.remove('text-success');
                netProfitElement.classList.add('text-danger');
            } else {
                netProfitElement.classList.remove('text-success', 'text-danger');
            }
        }

        // Advanced Profit Analytics (Guarded)
        const elTotalTradeProfit = document.getElementById('totalTradeProfit');
        if (elTotalTradeProfit) elTotalTradeProfit.textContent = `$${(data.total_trade_profit || 0).toFixed(2)}`;

        const elTotalTradeLoss = document.getElementById('totalTradeLoss');
        if (elTotalTradeLoss) elTotalTradeLoss.textContent = `$${(data.total_trade_loss || 0).toFixed(2)}`;

        const elNetTradeProfit = document.getElementById('netTradeProfit');
        if (elNetTradeProfit) elNetTradeProfit.textContent = `$${(data.net_trade_profit || 0).toFixed(2)}`;

        const elTotalTrades = document.getElementById('totalTrades');
        if (elTotalTrades) elTotalTrades.textContent = data.total_trades !== undefined ? data.total_trades : '0';

        // Update daily report if present
        if (data.daily_reports) {
            updateDailyReport(data.daily_reports);
        }
        // Use Backend-provided fee metrics (Centralized Logic)
        const tradeFees = data.trade_fees || 0;
        const usedFee = data.used_fees || 0;
        const remainingFee = (data.remaining_amount || 0) * ((currentConfig?.trade_fee_percentage || 0.07) / 100); // Remaining is still estimate
        const sizeFee = data.size_fees || 0;
        const feeRate = (currentConfig?.trade_fee_percentage || 0.07);

        const elTradeFees = document.getElementById('tradeFees');
        if (elTradeFees) elTradeFees.textContent = `$${Number(tradeFees).toFixed(2)}`;

        const elUsedFee = document.getElementById('usedFee');
        if (elUsedFee) elUsedFee.textContent = `$${Number(usedFee).toFixed(2)}`;

        const elRemainingFee = document.getElementById('remainingFee');
        if (elRemainingFee) elRemainingFee.textContent = `$${Number(remainingFee).toFixed(2)}`;

        const elFeeRateDisplay = document.getElementById('feeRateDisplay');
        if (elFeeRateDisplay) elFeeRateDisplay.textContent = `${Number(feeRate).toFixed(3)}%`;

        const elSizeAmountDisplay = document.getElementById('sizeAmountDisplay');
        if (elSizeAmountDisplay) elSizeAmountDisplay.textContent = `$${Number(data.size_amount || 0).toFixed(2)}`;

        const elSizeFeeDisplay = document.getElementById('sizeFeeDisplay');
        if (elSizeFeeDisplay) elSizeFeeDisplay.textContent = `$${Number(sizeFee).toFixed(2)}`;

        lastUsedFee = usedFee;
        lastSizeFee = sizeFee;

        // UI Color Feedback for Need Add
        const needAddTgt = data.need_add_usdt || 0;
        const needAddAboveZero = data.need_add_above_zero || 0;

        const tgtEl = document.getElementById('needAddProfitTargetDisplay');
        const zeroEl = document.getElementById('needAddAboveZeroDisplay');

        if (tgtEl) {
            if (needAddTgt > 0) {
                if (tgtEl.parentElement) tgtEl.parentElement.classList.add('bg-warning-subtle');
                tgtEl.classList.add('text-warning');
            } else {
                if (tgtEl.parentElement) tgtEl.parentElement.classList.remove('bg-warning-subtle');
                tgtEl.classList.remove('text-warning');
            }
        }

        if (zeroEl) {
            if (needAddAboveZero > 0) {
                if (zeroEl.parentElement) zeroEl.parentElement.classList.add('bg-warning-subtle');
                zeroEl.classList.add('text-warning');
            } else {
                if (zeroEl.parentElement) zeroEl.parentElement.classList.remove('bg-warning-subtle');
                zeroEl.classList.remove('text-warning');
            }
        }

        updateAutoCalDisplay();
    } catch (e) {
        console.error("Error in updateAccountMetrics:", e);
    }
}

function updateDailyReport(reports) {
    const tableBody = document.getElementById('dailyReportTableBody');
    if (!tableBody) return;

    if (!reports || reports.length === 0) {
        tableBody.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-4">No report data available yet.</td></tr>';
        return;
    }

    // Sort reports by date descending
    const sortedReports = [...reports].sort((a, b) => b.date.localeCompare(a.date));

    tableBody.innerHTML = sortedReports.map(report => `
        <tr>
            <td>${report.date}</td>
            <td>$${report.total_capital.toFixed(2)}</td>
            <td class="${report.net_trade_profit >= 0 ? 'text-success' : 'text-danger'}">
                $${report.net_trade_profit.toFixed(2)}
            </td>
            <td>
                <span class="badge ${report.compound_interest >= 1 ? 'bg-success' : 'bg-danger'}">
                    ${((report.compound_interest - 1) * 100).toFixed(2)}%
                </span>
                <small class="text-muted ms-1">(${report.compound_interest.toFixed(4)})</small>
            </td>
        </tr>
    `).join('');
}

function updateAutoCalDisplay() {
    // Profit
    const profitTimes = parseFloat(document.getElementById('pnlAutoCalTimes').value) || 0;
    const autoProfitValue = lastUsedFee * profitTimes;
    const profitDisplay = document.getElementById('pnlAutoCalDisplay');
    if (profitDisplay) profitDisplay.value = autoProfitValue.toFixed(2);

    // Loss
    const lossTimes = parseFloat(document.getElementById('pnlAutoCalLossTimes').value) || 0;
    const autoLossValue = -(lastUsedFee * lossTimes);
    const lossDisplay = document.getElementById('pnlAutoCalLossDisplay');
    if (lossDisplay) lossDisplay.value = autoLossValue.toFixed(2);

    // Auto-Cal Size (Profit) (NEW)
    const sizeProfitTimes = parseFloat(document.getElementById('sizeAutoCalTimes').value) || 0;
    // Uses Size Fee basis as requested
    const autoSizeProfitValue = lastSizeFee * sizeProfitTimes;
    const sizeProfitDisplay = document.getElementById('sizeAutoCalDisplay');
    if (sizeProfitDisplay) sizeProfitDisplay.value = autoSizeProfitValue.toFixed(2);

    // Auto-Cal Size Loss (NEW)
    const sizeLossTimes = parseFloat(document.getElementById('sizeAutoCalLossTimes').value) || 0;
    const autoSizeLossValue = -(lastSizeFee * sizeLossTimes);
    const sizeLossDisplay = document.getElementById('sizeAutoCalLossDisplay');
    if (sizeLossDisplay) sizeLossDisplay.value = autoSizeLossValue.toFixed(2);
}

function updatePositionDisplay(positionData) {
    const mlResultsContainer = document.getElementById('mlStrategyResults');

    if (!positionData || (!positionData.in_position && (!positionData.positions || (!positionData.positions.long.in && !positionData.positions.short.in)))) {
        mlResultsContainer.innerHTML = '<p class="text-muted">No active position.</p>';
        return;
    }

    let positionsToRender = [];

    if (positionData.positions) {
        if (positionData.positions.long.in) {
            positionsToRender.push({ side: 'LONG', ...positionData.positions.long });
        }
        if (positionData.positions.short.in) {
            positionsToRender.push({ side: 'SHORT', ...positionData.positions.short });
        }
    } else if (positionData.in_position) {
        // Fallback for older data format or primary display
        positionsToRender.push({
            side: (positionData.position_qty > 0 ? 'LONG' : 'SHORT'),
            price: positionData.position_entry_price,
            qty: positionData.position_qty,
            tp: positionData.current_take_profit,
            sl: positionData.current_stop_loss
        });
    }

    let positionHtml = '';
    positionsToRender.forEach(pos => {
        positionHtml += `
            <div class="position-card mb-2 p-2 border rounded ${pos.side.toLowerCase()}-bg">
                <div class="d-flex justify-content-between align-items-center mb-1">
                    <h6 class="mb-0 text-${pos.side === 'LONG' ? 'success' : 'danger'} font-weight-bold">${pos.side} POSITION</h6>
                    <span class="badge bg-${pos.side === 'LONG' ? 'success' : 'danger'}">Active</span>
                </div>
                <div class="row g-0">
                    <div class="col-6 small text-white-50">Entry Price:</div>
                    <div class="col-6 small text-end">${Number(pos.price || 0).toFixed(4)}</div>
                    <div class="col-6 small text-white-50">Quantity:</div>
                    <div class="col-6 small text-end">${Number(pos.qty || 0).toFixed(4)}</div>
                    <div class="col-6 small text-white-50">Current TP:</div>
                    <div class="col-6 small text-end text-success">${Number(pos.tp || (positionData && positionData.current_take_profit) || 0).toFixed(4)}</div>
                    <div class="col-6 small text-white-50">Current SL:</div>
                    <div class="col-6 small text-end text-danger">${Number(pos.sl || (positionData && positionData.current_stop_loss) || 0).toFixed(4)}</div>
                </div>
            </div>
        `;
    });
    mlResultsContainer.innerHTML = positionHtml;

    // Update Liq Gap Display
    const liqGapDisplay = document.getElementById('liqGapDisplay');
    if (liqGapDisplay) {
        let minGap = Infinity;
        positionsToRender.forEach(pos => {
            const liqp = parseFloat(pos.liq || 0);
            const sl = parseFloat(pos.sl || (positionData && positionData.current_stop_loss) || 0);
            if (liqp > 0 && sl > 0) {
                const gap = Math.abs(sl - liqp);
                if (gap < minGap) minGap = gap;
            }
        });

        if (minGap === Infinity) {
            liqGapDisplay.textContent = '$0.00';
            liqGapDisplay.className = 'badge bg-dark';
        } else {
            liqGapDisplay.textContent = `$${minGap.toFixed(2)}`;
            // Color code based on danger (e.g. if gap < autoMarginOffset or just generic warning)
            const offset = parseFloat(document.getElementById('autoMarginOffset').value) || 30;
            if (minGap < offset) {
                liqGapDisplay.className = 'badge bg-danger';
            } else if (minGap < offset * 2) {
                liqGapDisplay.className = 'badge bg-warning text-dark';
            } else {
                liqGapDisplay.className = 'badge bg-success';
            }
        }
    }
}

function updateParametersDisplay() {
    const paramsContainer = document.getElementById('currentParams');
    if (currentConfig) {
        let configHtml = `
           <div class="param-item">
               <span class="param-label">Symbol:</span>
               <span class="param-value">${currentConfig.symbol}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Direction:</span>
               <span class="param-value">${currentConfig.direction}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Mode:</span>
               <span class="param-value">${currentConfig.mode}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Leverage:</span>
               <span class="param-value">${currentConfig.leverage}x</span>
           </div>
           <div class="param-item">
               <span class="param-label">Max Allowed Used (USDT):</span>
               <span class="param-value">${currentConfig.max_allowed_used}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Target Order Amount:</span>
               <span class="param-value">${currentConfig.target_order_amount}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Min Order Amount:</span>
               <span class="param-value">${currentConfig.min_order_amount}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Entry Price Offset:</span>
               <span class="param-value">${currentConfig.entry_price_offset}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Batch Offset:</span>
               <span class="param-value">${currentConfig.batch_offset}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Batch Size Per Loop:</span>
               <span class="param-value">${currentConfig.batch_size_per_loop}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Loop Time:</span>
               <span class="param-value">${currentConfig.loop_time_seconds}s</span>
           </div>
           <div class="param-item">
               <span class="param-label">Rate Divisor:</span>
               <span class="param-value">${currentConfig.rate_divisor}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Short Safety Line Price:</span>
               <span class="param-value">${currentConfig.short_safety_line_price}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Long Safety Line Price:</span>
               <span class="param-value">${currentConfig.long_safety_line_price}</span>
           </div>
           <div class="param-item">
               <span class="param-label">TP Price Offset:</span>
               <span class="param-value">${currentConfig.tp_price_offset}</span>
           </div>
           <div class="param-item">
               <span class="param-label">SL Price Offset:</span>
               <span class="param-value">${currentConfig.sl_price_offset}</span>
           </div>
           <div class="param-item">
               <span class="param-label">TP Amount (%):</span>
               <span class="param-value">${currentConfig.tp_amount}</span>
           </div>
           <div class="param-item">
               <span class="param-label">SL Amount (%):</span>
               <span class="param-value">${currentConfig.sl_amount}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Trigger Price:</span>
               <span class="param-value">${currentConfig.trigger_price}</span>
           </div>
           <div class="param-item">
               <span class="param-label">TP Mode:</span>
               <span class="param-value">${currentConfig.tp_mode}</span>
           </div>
           <div class="param-item">
               <span class="param-label">TP Type:</span>
               <span class="param-value">${currentConfig.tp_type}</span>
           </div>
            <div class="param-item">
                <span class="param-label">Trade Fee %:</span>
                <span class="param-value">${currentConfig.trade_fee_percentage}%</span>
            </div>
            <div class="param-item">
                <span class="param-label">Cancel Unfilled (s):</span>
                <span class="param-value">${currentConfig.cancel_unfilled_seconds}</span>
            </div>
            <div class="param-item">
                <span class="param-label">Short: Cancel if TP below market:</span>
                <span class="param-value">${currentConfig.cancel_on_tp_price_below_market ? 'Yes' : 'No'}</span>
            </div>
            <div class="param-item">
                <span class="param-label">Short: Cancel if Entry below market:</span>
                <span class="param-value">${currentConfig.cancel_on_entry_price_below_market ? 'Yes' : 'No'}</span>
            </div>
            <div class="param-item">
                <span class="param-label">Long: Cancel if Entry above market:</span>
                <span class="param-value">${currentConfig.cancel_on_entry_price_above_market ? 'Yes' : 'No'}</span>
            </div>
           <div class="param-item">
               <span class="param-label">Use Candlestick Conditions:</span>
               <span class="param-value">${currentConfig.use_candlestick_conditions ? 'Yes' : 'No'}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Candlestick Timeframe:</span>
               <span class="param-value">${currentConfig.candlestick_timeframe}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Use Chg Open/Close:</span>
               <span class="param-value">${currentConfig.use_chg_open_close ? 'Yes' : 'No'}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Min Chg Open/Close:</span>
               <span class="param-value">${currentConfig.min_chg_open_close}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Max Chg Open/Close:</span>
               <span class="param-value">${currentConfig.max_chg_open_close}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Use Chg High/Low:</span>
               <span class="param-value">${currentConfig.use_chg_high_low ? 'Yes' : 'No'}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Min Chg High/Low:</span>
               <span class="param-value">${currentConfig.min_chg_high_low}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Max Chg High/Low:</span>
               <span class="param-value">${currentConfig.max_chg_high_low}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Use Chg High/Close:</span>
               <span class="param-value">${currentConfig.use_chg_high_close ? 'Yes' : 'No'}</span>
           </div>
           <div class="param-item">
               <span class="param-label">Min Chg High/Close:</span>
               <span class="param-value">${currentConfig.min_chg_high_close}</span>
           </div>
            <div class="param-item">
                <span class="param-label">Max Chg High/Close:</span>
                <span class="param-value">${currentConfig.max_chg_high_close}</span>
            </div>
            <div class="param-item pb-1 border-bottom border-secondary border-opacity-25 mb-1">
                <span class="param-label text-info">Secondary Gap:</span>
                <span class="param-value">${currentConfig.add_pos_gap_threshold_2 || currentConfig.add_pos_gap_threshold || '5.0'}</span>
            </div>
            <div class="param-item">
                <span class="param-label text-info">Secondary Size %:</span>
                <span class="param-value">${currentConfig.add_pos_size_pct_2 || currentConfig.add_pos_size_pct || '30'}%</span>
            </div>
       `;
        paramsContainer.innerHTML = configHtml;
    } else {
        paramsContainer.innerHTML = '<p class="text-muted">No parameters loaded yet.</p>';
    }
}

function updateOpenTrades(trades) {
    const tradesContainer = document.getElementById('openTrades');

    if (!trades || trades.length === 0) {
        tradesContainer.innerHTML = '<p class="text-muted">No open positions</p>';
        return;
    }

    tradesContainer.innerHTML = trades.map(trade => {
        // Cache the expiration target timestamp to avoid server stutter
        if (trade.time_left !== null) {
            orderExpirationCache[trade.id] = Date.now() + (trade.time_left * 1000);
        } else {
            delete orderExpirationCache[trade.id];
        }

        return `
        <div class="trade-card ${trade.type.toLowerCase()}">
            <div class="trade-header">
                <span class="trade-type ${trade.type.toLowerCase()}">${trade.type}</span>
                <span class="trade-id">ID: ${trade.id} <span class="badge bg-warning text-dark ms-1 timer-badge" data-order-id="${trade.id}">${trade.time_left !== null ? trade.time_left + 's' : ''}</span></span>
            </div>
            <div class="trade-details">
                <div class="trade-detail-item">
                    <span class="trade-detail-label">Entry:</span>
                    <span class="trade-detail-value">${trade.entry_spot_price !== null ? Number(trade.entry_spot_price).toFixed(4) : 'N/A'}</span>
                </div>
                <div class="trade-detail-item">
                    <span class="param-label">Target Order:</span>
                    <span class="param-value">$${trade.stake !== null ? Number(trade.stake).toFixed(2) : 'N/A'}</span>
                </div>
                <div class="trade-detail-item">
                    <span class="trade-detail-label">TP:</span>
                    <span class="trade-detail-value text-success">${trade.tp_price !== null ? Number(trade.tp_price).toFixed(4) : 'N/A'}</span>
                </div>
                <div class="trade-detail-item">
                    <span class="trade-detail-label">SL:</span>
                    <span class="trade-detail-value text-danger">${trade.sl_price !== null ? Number(trade.sl_price).toFixed(4) : 'N/A'}</span>
                </div>
            </div>
        </div>
    `;
    }).join('');
}

function startUITimers() {
    // Precise local countdown timer
    setInterval(() => {
        const now = Date.now();
        const badges = document.querySelectorAll('.timer-badge');
        badges.forEach(badge => {
            const orderId = badge.getAttribute('data-order-id');
            const targetTime = orderExpirationCache[orderId];

            if (targetTime) {
                const remaining = Math.max(0, Math.floor((targetTime - now) / 1000));
                badge.textContent = remaining + 's';

                // Cleanup cache if reached 0
                if (remaining <= 0) delete orderExpirationCache[orderId];
            }
        });
    }, 1000);
}

function addConsoleLog(log) {
    const consoleOutput = document.getElementById('consoleOutput');

    if (consoleOutput.querySelector('.text-muted')) {
        consoleOutput.innerHTML = '';
    }

    const logLine = document.createElement('div');
    logLine.className = `console-line ${log.level}`;
    logLine.innerHTML = `
        <span class="console-timestamp">[${log.timestamp}]</span>
        <span class="console-message">${escapeHtml(log.message)}</span>
    `;

    // Check if user is near the bottom
    const isAtBottom = consoleOutput.scrollHeight - consoleOutput.clientHeight <= consoleOutput.scrollTop + 50;

    consoleOutput.appendChild(logLine);

    if (isAtBottom) {
        consoleOutput.scrollTop = consoleOutput.scrollHeight;
    }

    if (consoleOutput.children.length > 500) {
        consoleOutput.removeChild(consoleOutput.firstChild);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function loadConfig() {
    try {
        const response = await fetch('/api/config');
        currentConfig = await response.json();

        // Sync PnL Auto-Cancel UI (New Dual Mode)
        // Auto-Manual
        const useAutoManual = currentConfig.use_pnl_auto_manual !== undefined ? currentConfig.use_pnl_auto_manual : false;
        const manualThreshold = currentConfig.pnl_auto_manual_threshold !== undefined ? currentConfig.pnl_auto_manual_threshold : 100.0;

        const elUseManual = document.getElementById('usePnlAutoManual');
        if (elUseManual) elUseManual.checked = useAutoManual;

        const elManualThresh = document.getElementById('pnlAutoManualThreshold');
        if (elManualThresh) elManualThresh.value = manualThreshold;

        // Auto-Cal
        const useAutoCal = currentConfig.use_pnl_auto_cal !== undefined ? currentConfig.use_pnl_auto_cal : false;
        const calTimes = currentConfig.pnl_auto_cal_times !== undefined ? currentConfig.pnl_auto_cal_times : 4.0;

        const elUseCal = document.getElementById('usePnlAutoCal');
        if (elUseCal) elUseCal.checked = useAutoCal;

        const elCalTimes = document.getElementById('pnlAutoCalTimes');
        if (elCalTimes) elCalTimes.value = calTimes;

        // Auto-Cal Loss
        const useAutoCalLoss = currentConfig.use_pnl_auto_cal_loss !== undefined ? currentConfig.use_pnl_auto_cal_loss : false;
        const calLossTimes = currentConfig.pnl_auto_cal_loss_times !== undefined ? currentConfig.pnl_auto_cal_loss_times : 1.5;

        const elUseCalLoss = document.getElementById('usePnlAutoCalLoss');
        if (elUseCalLoss) elUseCalLoss.checked = useAutoCalLoss;

        const elCalLossTimes = document.getElementById('pnlAutoCalLossTimes');
        if (elCalLossTimes) elCalLossTimes.value = calLossTimes;

        // Auto-Cal Size (Profit)
        const useSizeAutoCal = currentConfig.use_size_auto_cal !== undefined ? currentConfig.use_size_auto_cal : false;
        const sizeCalTimes = currentConfig.size_auto_cal_times !== undefined ? currentConfig.size_auto_cal_times : 2.0;

        const elUseSizeCal = document.getElementById('useSizeAutoCal');
        if (elUseSizeCal) elUseSizeCal.checked = useSizeAutoCal;

        const elSizeCalTimes = document.getElementById('sizeAutoCalTimes');
        if (elSizeCalTimes) elSizeCalTimes.value = sizeCalTimes;

        // Auto-Cal Size Loss
        const useSizeAutoCalLoss = currentConfig.use_size_auto_cal_loss !== undefined ? currentConfig.use_size_auto_cal_loss : false;
        const sizeCalLossTimes = currentConfig.size_auto_cal_loss_times !== undefined ? currentConfig.size_auto_cal_loss_times : 1.5;

        const elUseSizeCalLoss = document.getElementById('useSizeAutoCalLoss');
        if (elUseSizeCalLoss) elUseSizeCalLoss.checked = useSizeAutoCalLoss;

        const elSizeCalLossTimes = document.getElementById('sizeAutoCalLossTimes');
        if (elSizeCalLossTimes) elSizeCalLossTimes.value = sizeCalLossTimes;

        // Auto-Add Margin
        const useAutoMargin = currentConfig.use_auto_margin !== undefined ? currentConfig.use_auto_margin : false;
        const autoMarginOffset = currentConfig.auto_margin_offset !== undefined ? currentConfig.auto_margin_offset : 30.0;

        const elUseAutoMargin = document.getElementById('useAutoMargin');
        if (elUseAutoMargin) elUseAutoMargin.checked = useAutoMargin;

        const elAutoMarginOffset = document.getElementById('autoMarginOffset');
        if (elAutoMarginOffset) elAutoMarginOffset.value = autoMarginOffset;

        // Auto-Cal Add Position
        const useAddPos = currentConfig.use_add_pos_auto_cal !== undefined ? currentConfig.use_add_pos_auto_cal : false;
        const addPosRecovery = currentConfig.add_pos_recovery_percent !== undefined ? currentConfig.add_pos_recovery_percent : 0.7;

        const elAboveZero = document.getElementById('useAddPosAboveZero');
        if (elAboveZero) elAboveZero.checked = currentConfig.use_add_pos_above_zero || false;
        const elAboveZeroMain = document.getElementById('useAddPosAboveZeroMain');
        if (elAboveZeroMain) elAboveZeroMain.checked = currentConfig.use_add_pos_above_zero || false;

        const elProfitTarget = document.getElementById('useAddPosProfitTarget');
        if (elProfitTarget) elProfitTarget.checked = currentConfig.use_add_pos_profit_target || currentConfig.use_add_pos_auto_cal || false;
        const elProfitTargetMain = document.getElementById('useAddPosProfitTargetMain');
        if (elProfitTargetMain) elProfitTargetMain.checked = currentConfig.use_add_pos_profit_target || currentConfig.use_add_pos_auto_cal || false;

        const elAddPosRecovery = document.getElementById('addPosRecoveryPercent');
        if (elAddPosRecovery) elAddPosRecovery.value = addPosRecovery;
        const elAddPosRecoveryMain = document.getElementById('addPosRecoveryPercentMain');
        if (elAddPosRecoveryMain) elAddPosRecoveryMain.value = addPosRecovery;

        const addPosProfitMult = currentConfig.add_pos_profit_multiplier !== undefined ? currentConfig.add_pos_profit_multiplier : 1.5;
        const elAddPosProfitMult = document.getElementById('addPosProfitMultiplier');
        if (elAddPosProfitMult) elAddPosProfitMult.value = addPosProfitMult;
        const elAddPosProfitMultMain = document.getElementById('addPosProfitMultiplierMain');
        if (elAddPosProfitMultMain) elAddPosProfitMultMain.value = addPosProfitMult;

        const addPosGap = currentConfig.add_pos_gap_threshold !== undefined ? currentConfig.add_pos_gap_threshold : 5.0;
        const elAddPosGap = document.getElementById('addPosGapThreshold');
        if (elAddPosGap) elAddPosGap.value = addPosGap;
        const elAddPosGapMain = document.getElementById('addPosGapThresholdMain');
        if (elAddPosGapMain) elAddPosGapMain.value = addPosGap;

        const step2Offset = currentConfig.add_pos_step2_offset !== undefined ? currentConfig.add_pos_step2_offset : 0.0;
        const elStep2Offset = document.getElementById('addPosStep2Offset');
        if (elStep2Offset) elStep2Offset.value = step2Offset;

        // New Percentage Based Martingale Fields
        const addPosSizePct = currentConfig.add_pos_size_pct !== undefined ? currentConfig.add_pos_size_pct : 30.0;
        const elSizePct = document.getElementById('addPosSizePct');
        if (elSizePct) elSizePct.value = addPosSizePct;
        const elSizePctMain = document.getElementById('addPosSizePctMain');
        if (elSizePctMain) elSizePctMain.value = addPosSizePct;

        const addPosMaxCount = currentConfig.add_pos_max_count !== undefined ? currentConfig.add_pos_max_count : 10;
        const elMaxCount = document.getElementById('addPosMaxCount');
        if (elMaxCount) elMaxCount.value = addPosMaxCount;
        const elMaxCountMain = document.getElementById('addPosMaxCountMain');
        if (elMaxCountMain) elMaxCountMain.value = addPosMaxCount;

        // Sequential Offsets (Step 2+)
        const gap2 = currentConfig.add_pos_gap_threshold_2 !== undefined ? currentConfig.add_pos_gap_threshold_2 : addPosGap;
        const elGap2 = document.getElementById('addPosGapThreshold2');
        if (elGap2) elGap2.value = gap2;
        const elGap2Main = document.getElementById('addPosGapThreshold2Main');
        if (elGap2Main) elGap2Main.value = gap2;

        const sizePct2 = currentConfig.add_pos_size_pct_2 !== undefined ? currentConfig.add_pos_size_pct_2 : addPosSizePct;
        const elSizePct2 = document.getElementById('addPosSizePct2');
        if (elSizePct2) elSizePct2.value = sizePct2;
        const elSizePct2Main = document.getElementById('addPosSizePct2Main');
        if (elSizePct2Main) elSizePct2Main.value = sizePct2;

        // Dashboard Security
        const elDashUser = document.getElementById('dashboardUsername');
        if (elDashUser) elDashUser.value = currentConfig.dashboard_username || '';
        const elDashPass = document.getElementById('dashboardPassword');
        if (elDashPass) elDashPass.value = currentConfig.dashboard_password || '';

        // Add live listeners if not already added
        const fieldsToWatch = [
            'useAddPosAboveZero', 'useAddPosProfitTarget', 'addPosRecoveryPercent',
            'addPosProfitMultiplier', 'addPosGapThreshold', 'addPosSizePct', 'addPosMaxCount',
            'addPosGapThreshold2', 'addPosSizePct2'
        ];

        fieldsToWatch.forEach(fieldId => {
            const camelCaseId = fieldId.replace(/_([a-z])/g, (g) => g[1].toUpperCase());

            // Check base ID (modal)
            const input = document.getElementById(camelCaseId);
            if (input && !input.dataset.listener) {
                input.addEventListener('change', saveLiveConfigs);
                input.dataset.listener = 'true';
            }

            // Check Main ID (dashboard)
            const inputMain = document.getElementById(camelCaseId + 'Main');
            if (inputMain && !inputMain.dataset.listener) {
                inputMain.addEventListener('change', saveLiveConfigs);
                inputMain.dataset.listener = 'true';
            }
        });

        // Legacy fallback
        if (currentConfig.use_pnl_auto_cancel !== undefined && currentConfig.use_pnl_auto_manual === undefined) {
            if (elUseManual) elUseManual.checked = currentConfig.use_pnl_auto_cancel;
            if (elManualThresh) elManualThresh.value = currentConfig.pnl_auto_cancel_threshold;
        }

        // Initial dashboard trade fee % sync
        const feeInput = document.getElementById('tradeFeePercentage');
        if (feeInput) {
            feeInput.value = currentConfig.trade_fee_percentage !== undefined ? currentConfig.trade_fee_percentage : 0.07;
        }
        updateAutoCalDisplay();
    } catch (error) {
        console.error('Error loading config:', error);
        showNotification('Failed to load configuration', 'error');
    }
}

async function loadStatus() {
    try {
        const response = await fetch('/api/status');
        const status = await response.json();

        try { updateBotStatus(status.running); } catch (e) { console.error("Error updating bot status:", e); }
        try { updateAccountMetrics(status); } catch (e) { console.error("Error updating account metrics:", e); }
        try { updateOpenTrades(status.open_trades); } catch (e) { console.error("Error updating open trades:", e); }
        try { updatePositionDisplay(status); } catch (e) { console.error("Error updating position display:", e); }
        try { updateParametersDisplay(); } catch (e) { console.error("Error updating parameters display:", e); }
    } catch (error) {
        console.error('Error loading status:', error);
    }
}

function loadConfigToModal() {
    if (!currentConfig) return;

    const setVal = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.value = val === undefined || val === null ? '' : val;
    };
    const setChecked = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.checked = !!val;
    };

    setVal('okxApiKey', currentConfig.okx_api_key);
    setVal('okxApiSecret', currentConfig.okx_api_secret);
    setVal('okxPassphrase', currentConfig.okx_passphrase);
    setVal('okxDemoApiKey', currentConfig.okx_demo_api_key);
    setVal('okxDemoApiSecret', currentConfig.okx_demo_api_secret);
    setVal('okxDemoApiPassphrase', currentConfig.okx_demo_api_passphrase);
    setVal('devApiKey', currentConfig.dev_api_key);
    setVal('devApiSecret', currentConfig.dev_api_secret);
    setVal('devPassphrase', currentConfig.dev_passphrase);
    setVal('devDemoApiKey', currentConfig.dev_demo_api_key);
    setVal('devDemoApiSecret', currentConfig.dev_demo_api_secret);
    setVal('devDemoApiPassphrase', currentConfig.dev_demo_api_passphrase);
    setChecked('useDeveloperApi', currentConfig.use_developer_api);
    setChecked('useTestnet', currentConfig.use_testnet);
    setVal('symbol', currentConfig.symbol);
    setVal('shortSafetyLinePrice', currentConfig.short_safety_line_price);
    setVal('longSafetyLinePrice', currentConfig.long_safety_line_price);
    setVal('leverage', currentConfig.leverage);
    setVal('maxAllowedUsed', currentConfig.max_allowed_used);
    setVal('entryPriceOffset', currentConfig.entry_price_offset);
    setVal('batchOffset', currentConfig.batch_offset);
    setVal('tpPriceOffset', currentConfig.tp_price_offset);
    setVal('slPriceOffset', currentConfig.sl_price_offset);
    setVal('loopTimeSeconds', currentConfig.loop_time_seconds);
    setVal('rateDivisor', currentConfig.rate_divisor);
    setVal('batchSizePerLoop', currentConfig.batch_size_per_loop);
    setVal('minOrderAmount', currentConfig.min_order_amount);
    setVal('targetOrderAmount', currentConfig.target_order_amount);
    setVal('cancelUnfilledSeconds', currentConfig.cancel_unfilled_seconds);
    setChecked('cancelOnEntryPriceBelowMarket', currentConfig.cancel_on_entry_price_below_market);
    setChecked('cancelOnEntryPriceAboveMarket', currentConfig.cancel_on_entry_price_above_market);
    setVal('tradeFeePercentage', currentConfig.trade_fee_percentage || 0.07);

    setVal('direction', currentConfig.direction);
    setVal('mode', currentConfig.mode);
    setVal('tpAmount', currentConfig.tp_amount);
    setVal('slAmount', currentConfig.sl_amount);
    setVal('triggerPrice', currentConfig.trigger_price);
    setVal('tpMode', currentConfig.tp_mode);
    setVal('tpType', currentConfig.tp_type);
    setChecked('useCandlestickConditions', currentConfig.use_candlestick_conditions);

    setChecked('useChgOpenClose', currentConfig.use_chg_open_close);
    setVal('minChgOpenClose', currentConfig.min_chg_open_close);
    setVal('maxChgOpenClose', currentConfig.max_chg_open_close);
    setChecked('useChgHighLow', currentConfig.use_chg_high_low);
    setVal('minChgHighLow', currentConfig.min_chg_high_low);
    setVal('maxChgHighLow', currentConfig.max_chg_high_low);
    setChecked('useChgHighClose', currentConfig.use_chg_high_close);
    setVal('minChgHighClose', currentConfig.min_chg_high_close);
    setVal('maxChgHighClose', currentConfig.max_chg_high_close);
    setVal('candlestickTimeframe', currentConfig.candlestick_timeframe);
    setVal('okxPosMode', currentConfig.okx_pos_mode || 'net_mode');

    setChecked('usePnlAutoCancelModal', currentConfig.use_pnl_auto_manual);
    setVal('pnlAutoCancelThresholdModal', currentConfig.pnl_auto_manual_threshold || 100.0);

    setVal('addPosRecoveryPercent', currentConfig.add_pos_recovery_percent || 0.6);
    setVal('addPosGapThreshold', currentConfig.add_pos_gap_threshold || 5.0);
    setVal('addPosProfitMultiplier', currentConfig.add_pos_profit_multiplier || 1.5);
    setVal('addPosSizePct', currentConfig.add_pos_size_pct || 30.0);
    setVal('addPosMaxCount', currentConfig.add_pos_max_count || 10);
    setVal('addPosGapThreshold2', currentConfig.add_pos_gap_threshold_2 || currentConfig.add_pos_gap_threshold || 5.0);
    setVal('addPosSizePct2', currentConfig.add_pos_size_pct_2 || currentConfig.add_pos_size_pct || 30.0);
    setVal('dashboardUsername', currentConfig.dashboard_username || '');
    setVal('dashboardPassword', currentConfig.dashboard_password || '');
}

// Helper to keep dashboard and modal in sync - Removed old PnL sync listeners as modal update is pending
// TODO: Update modal with new fields if needed.

async function saveConfig() {
    const getValue = (id, defaultValue = '') => {
        const el = document.getElementById(id);
        if (!el) {
            console.warn(`Element with ID "${id}" not found in DOM.`);
            return defaultValue;
        }
        return el.value;
    };

    const getFloat = (id, defaultValue = 0) => {
        const val = getValue(id);
        const parsed = parseFloat(val);
        return isNaN(parsed) ? defaultValue : parsed;
    };

    const getInt = (id, defaultValue = 0) => {
        const val = getValue(id);
        const parsed = parseInt(val);
        return isNaN(parsed) ? defaultValue : parsed;
    };

    const getChecked = (id, defaultValue = false) => {
        const el = document.getElementById(id);
        if (!el) {
            console.warn(`Element with ID "${id}" not found in DOM.`);
            return defaultValue;
        }
        return el.checked;
    };

    const newConfig = {
        okx_api_key: getValue('okxApiKey'),
        okx_api_secret: getValue('okxApiSecret'),
        okx_passphrase: getValue('okxPassphrase'),
        okx_demo_api_key: getValue('okxDemoApiKey'),
        okx_demo_api_secret: getValue('okxDemoApiSecret'),
        okx_demo_api_passphrase: getValue('okxDemoApiPassphrase'),
        dev_api_key: getValue('devApiKey'),
        dev_api_secret: getValue('devApiSecret'),
        dev_passphrase: getValue('devPassphrase'),
        dev_demo_api_key: getValue('devDemoApiKey'),
        dev_demo_api_secret: getValue('devDemoApiSecret'),
        dev_demo_api_passphrase: getValue('devDemoApiPassphrase'),
        use_developer_api: getChecked('useDeveloperApi'),
        use_testnet: getChecked('useTestnet'),
        symbol: getValue('symbol'),
        short_safety_line_price: getFloat('shortSafetyLinePrice'),
        long_safety_line_price: getFloat('longSafetyLinePrice'),
        leverage: getInt('leverage'),
        max_allowed_used: getFloat('maxAllowedUsed'),
        entry_price_offset: getFloat('entryPriceOffset'),
        batch_offset: getFloat('batchOffset'),
        tp_price_offset: getFloat('tpPriceOffset'),
        sl_price_offset: getFloat('slPriceOffset'),
        loop_time_seconds: getInt('loopTimeSeconds'),
        rate_divisor: getInt('rateDivisor'),
        batch_size_per_loop: getInt('batchSizePerLoop'),
        min_order_amount: getFloat('minOrderAmount'),
        target_order_amount: getFloat('targetOrderAmount'),
        cancel_unfilled_seconds: getInt('cancelUnfilledSeconds'),
        cancel_on_entry_price_below_market: getChecked('cancelOnEntryPriceBelowMarket'),
        cancel_on_entry_price_above_market: getChecked('cancelOnEntryPriceAboveMarket'),
        trade_fee_percentage: getFloat('tradeFeePercentage'),

        direction: getValue('direction'),
        mode: getValue('mode'),
        tp_amount: getFloat('tpAmount'),
        sl_amount: getFloat('slAmount'),
        trigger_price: getValue('triggerPrice'),
        tp_mode: getValue('tpMode'),
        tp_type: getValue('tpType'),
        use_candlestick_conditions: getChecked('useCandlestickConditions'),

        use_chg_open_close: getChecked('useChgOpenClose'),
        min_chg_open_close: getFloat('minChgOpenClose'),
        max_chg_open_close: getFloat('maxChgOpenClose'),
        use_chg_high_low: getChecked('useChgHighLow'),
        min_chg_high_low: getFloat('minChgHighLow'),
        max_chg_high_low: getFloat('maxChgHighLow'),
        use_chg_high_close: getChecked('useChgHighClose'),
        min_chg_high_close: getFloat('minChgHighClose'),
        max_chg_high_close: getFloat('maxChgHighClose'),
        add_pos_gap_threshold: getFloat('addPosGapThreshold'),
        add_pos_profit_multiplier: getFloat('addPosProfitMultiplier'),
        add_pos_step2_offset: getFloat('addPosStep2Offset'),
        add_pos_size_pct: getFloat('addPosSizePct'),
        add_pos_max_count: getInt('addPosMaxCount'),
        add_pos_recovery_percent: getFloat('addPosRecoveryPercent'),
        add_pos_gap_threshold_2: getFloat('addPosGapThreshold2'),
        add_pos_size_pct_2: getFloat('addPosSizePct2'),
        dashboard_username: getValue('dashboardUsername'),
        dashboard_password: getValue('dashboardPassword'),
        use_add_pos_profit_target: getChecked('useAddPosProfitTarget'),

        candlestick_timeframe: getValue('candlestickTimeframe'),
        okx_pos_mode: getValue('okxPosMode'),

        use_pnl_auto_manual: document.getElementById('usePnlAutoCancelModal') ? document.getElementById('usePnlAutoCancelModal').checked : (document.getElementById('usePnlAutoManual') ? document.getElementById('usePnlAutoManual').checked : currentConfig.use_pnl_auto_manual),
        pnl_auto_manual_threshold: document.getElementById('pnlAutoCancelThresholdModal') ? parseFloat(document.getElementById('pnlAutoCancelThresholdModal').value) : (document.getElementById('pnlAutoManualThreshold') ? parseFloat(document.getElementById('pnlAutoManualThreshold').value) : currentConfig.pnl_auto_manual_threshold),
        use_pnl_auto_cal: document.getElementById('usePnlAutoCal') ? document.getElementById('usePnlAutoCal').checked : (currentConfig.use_pnl_auto_cal || false),
        pnl_auto_cal_times: document.getElementById('pnlAutoCalTimes') ? parseFloat(document.getElementById('pnlAutoCalTimes').value) : (currentConfig.pnl_auto_cal_times || 4.0),
        use_pnl_auto_cal_loss: document.getElementById('usePnlAutoCalLoss') ? document.getElementById('usePnlAutoCalLoss').checked : (currentConfig.use_pnl_auto_cal_loss || false),
        pnl_auto_cal_loss_times: document.getElementById('pnlAutoCalLossTimes') ? parseFloat(document.getElementById('pnlAutoCalLossTimes').value) : (currentConfig.pnl_auto_cal_loss_times || 1.5),

        use_size_auto_cal: document.getElementById('useSizeAutoCal') ? document.getElementById('useSizeAutoCal').checked : (currentConfig.use_size_auto_cal || false),
        size_auto_cal_times: document.getElementById('sizeAutoCalTimes') ? parseFloat(document.getElementById('sizeAutoCalTimes').value) : (currentConfig.size_auto_cal_times || 2.0),
        use_size_auto_cal_loss: document.getElementById('useSizeAutoCalLoss') ? document.getElementById('useSizeAutoCalLoss').checked : (currentConfig.use_size_auto_cal_loss || false),
        size_auto_cal_loss_times: document.getElementById('sizeAutoCalLossTimes') ? parseFloat(document.getElementById('sizeAutoCalLossTimes').value) : (currentConfig.size_auto_cal_loss_times || 1.5),

        use_add_pos_above_zero: document.getElementById('useAddPosAboveZero') ? document.getElementById('useAddPosAboveZero').checked : (currentConfig.use_add_pos_above_zero || false),
        add_pos_recovery_percent: getFloat('addPosRecoveryPercent'),
        add_pos_profit_multiplier: getFloat('addPosProfitMultiplier')
    };

    console.log('Saving config:', newConfig);

    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(newConfig),
        });

        const result = await response.json();

        if (result.success) {
            currentConfig = newConfig;
            configModal.hide();
            showNotification(result.message || 'Configuration saved successfully', 'success');
            updateParametersDisplay(); // Refresh the parameters display
        } else {
            showNotification(result.message, 'error');
        }
    } catch (error) {
        console.error('Error saving config:', error);
        showNotification('Failed to save configuration', 'error');
    }
}

// Function to save specific configs without closing modal (live updates)
async function saveLiveConfigs() {
    if (!currentConfig) return;

    const getVal = (id, def = '') => {
        const el = document.getElementById(id);
        return el ? el.value : (currentConfig[id] !== undefined ? currentConfig[id] : def);
    };
    const getChecked = (id, def = false) => {
        const el = document.getElementById(id);
        return el ? el.checked : (currentConfig[id] !== undefined ? currentConfig[id] : def);
    };

    const liveConfig = {
        use_pnl_auto_manual: getChecked('usePnlAutoManual'),
        pnl_auto_manual_threshold: parseFloat(getVal('pnlAutoManualThreshold', 100)),
        use_pnl_auto_cal: getChecked('usePnlAutoCal'),
        pnl_auto_cal_times: parseFloat(getVal('pnlAutoCalTimes', 4.0)),
        use_pnl_auto_cal_loss: getChecked('usePnlAutoCalLoss'),
        pnl_auto_cal_loss_times: parseFloat(getVal('pnlAutoCalLossTimes', 1.5)),

        use_size_auto_cal: getChecked('useSizeAutoCal'),
        size_auto_cal_times: parseFloat(getVal('sizeAutoCalTimes', 2.0)),
        use_size_auto_cal_loss: getChecked('useSizeAutoCalLoss'),
        size_auto_cal_loss_times: parseFloat(getVal('sizeAutoCalLossTimes', 1.5)),

        trade_fee_percentage: parseFloat(getVal('tradeFeePercentage', 0.07)),

        use_auto_margin: getChecked('useAutoMargin'),
        auto_margin_offset: parseFloat(getVal('autoMarginOffset', 10)),

        use_add_pos_above_zero: getChecked('useAddPosAboveZeroMain'),
        use_add_pos_profit_target: getChecked('useAddPosProfitTargetMain'),
        add_pos_recovery_percent: parseFloat(getVal('addPosRecoveryPercentMain', 0.6)),
        add_pos_profit_multiplier: parseFloat(getVal('addPosProfitMultiplierMain', 1.5)),
        add_pos_gap_threshold: parseFloat(getVal('addPosGapThresholdMain', 5.0)),
        add_pos_size_pct: parseFloat(getVal('addPosSizePctMain', 30.0)),
        add_pos_max_count: parseInt(getVal('addPosMaxCountMain', 10)),
        add_pos_step2_offset: parseFloat(getVal('addPosStep2OffsetMain', 0.0)), // Placeholder if missing
        add_pos_gap_threshold_2: parseFloat(getVal('addPosGapThreshold2Main', 5.0)),
        add_pos_size_pct_2: parseFloat(getVal('addPosSizePct2Main', 30.0))
    };

    try {
        const response = await fetch('/api/config', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(liveConfig)
        });
        const data = await response.json();
        if (data.success) {
            showNotification('Auto-Exit settings saved', 'success');
            // Update local currentConfig but don't reload everything
            currentConfig.use_pnl_auto_manual = liveConfig.use_pnl_auto_manual;
            currentConfig.pnl_auto_manual_threshold = liveConfig.pnl_auto_manual_threshold;
            currentConfig.use_pnl_auto_cal = liveConfig.use_pnl_auto_cal;
            currentConfig.pnl_auto_cal_times = liveConfig.pnl_auto_cal_times;
            currentConfig.use_pnl_auto_cal_loss = liveConfig.use_pnl_auto_cal_loss;
            currentConfig.pnl_auto_cal_loss_times = liveConfig.pnl_auto_cal_loss_times;
            currentConfig.trade_fee_percentage = liveConfig.trade_fee_percentage;

            // Update Size Cal in memory
            currentConfig.use_size_auto_cal = liveConfig.use_size_auto_cal;
            currentConfig.size_auto_cal_times = liveConfig.size_auto_cal_times;
            currentConfig.use_size_auto_cal_loss = liveConfig.use_size_auto_cal_loss;
            currentConfig.size_auto_cal_loss_times = liveConfig.size_auto_cal_loss_times;

            // Update Margin Cal in memory
            currentConfig.use_auto_margin = liveConfig.use_auto_margin;
            currentConfig.auto_margin_offset = liveConfig.auto_margin_offset;

            // Update Add Pos in memory
            currentConfig.use_add_pos_above_zero = liveConfig.use_add_pos_above_zero;
            currentConfig.use_add_pos_profit_target = liveConfig.use_add_pos_profit_target;
            currentConfig.add_pos_recovery_percent = liveConfig.add_pos_recovery_percent;
            currentConfig.add_pos_profit_multiplier = liveConfig.add_pos_profit_multiplier;
            currentConfig.add_pos_gap_threshold = liveConfig.add_pos_gap_threshold;
            currentConfig.add_pos_size_pct = liveConfig.add_pos_size_pct;
            currentConfig.add_pos_max_count = liveConfig.add_pos_max_count;
            currentConfig.add_pos_step2_offset = liveConfig.add_pos_step2_offset;
            currentConfig.add_pos_gap_threshold_2 = liveConfig.add_pos_gap_threshold_2;
            currentConfig.add_pos_size_pct_2 = liveConfig.add_pos_size_pct_2;
        } else {
            // Revert UI on error (e.g. bot running error)
            document.getElementById('usePnlAutoManual').checked = currentConfig.use_pnl_auto_manual;
            document.getElementById('pnlAutoManualThreshold').value = currentConfig.pnl_auto_manual_threshold;
            document.getElementById('usePnlAutoCal').checked = currentConfig.use_pnl_auto_cal;
            document.getElementById('pnlAutoCalTimes').value = currentConfig.pnl_auto_cal_times;
            document.getElementById('usePnlAutoCalLoss').checked = currentConfig.use_pnl_auto_cal_loss;
            document.getElementById('pnlAutoCalLossTimes').value = currentConfig.pnl_auto_cal_loss_times;
            document.getElementById('tradeFeePercentage').value = currentConfig.trade_fee_percentage;
            showNotification(data.message, 'error');
        }
    } catch (error) {
        console.error('Error saving live config:', error);
    }
}

function showNotification(message, type) {
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type === 'success' ? 'success' : 'danger'} alert-dismissible fade show position-fixed top-0 start-50 translate-middle-x mt-3`;
    alertDiv.style.zIndex = '10000'; // Increased z-index to ensure visibility
    alertDiv.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    `;

    document.body.appendChild(alertDiv);

    setTimeout(() => {
        alertDiv.remove();
    }, 5000);
}
