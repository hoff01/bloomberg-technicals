// State
const TRADE_MATH = window.TradeMath;
if (!TRADE_MATH) {
    throw new Error('Trade Builder math module failed to load. Expected trade_math.js before app.js.');
}

let state = {
    commodity: 'HO',
    unit: '$/bbl',
    years: [],
    field: 'last',
    monthSpecific: false,
    spreadMode: 'spreads',
    customSpreadName: '',
    prebuiltId: '',
    showVar: false,
    showTradingView: false,
    grayscale: false,
    dataSource: 'Embedded',
    dataUpdatedAt: null
};

const DEFAULT_BBL_PER_MT = 7.33;
const DEFAULT_GAL_PER_BBL = 42;
const UNIT_FACTORS_BY_CODE = {
    default: { bblPerMT: DEFAULT_BBL_PER_MT, galPerBbl: DEFAULT_GAL_PER_BBL }
};

const DEFAULT_TRADINGVIEW_SYMBOLS = {
    CL: 'NYMEX:CL',
    XB: 'NYMEX:RB',
    HO: 'NYMEX:HO',
    NG: 'NYMEX:NG',
    ME: 'NYMEX:ME',
    LT: 'NYMEX:LT'
};

const FALLBACK_ROOT_CONFIG = Object.freeze({
    WU: Object.freeze({
        root: 'WU',
        name: 'GC Jet',
        native_unit: 'cpg',
        yellow_key: 'Comdty',
        ticker_template: '{root}{month_code}{y} {yellow_key}',
        curve_mode: 'monthly',
        bbl_per_mt: DEFAULT_BBL_PER_MT,
        gal_per_bbl: DEFAULT_GAL_PER_BBL,
        aliases: ['ME', 'GC JET'],
        enabled: true
    }),
    HO: Object.freeze({
        root: 'HO',
        name: 'Heating Oil',
        native_unit: 'cpg',
        yellow_key: 'Comdty',
        ticker_template: '{root}{month_code}{y} {yellow_key}',
        curve_mode: 'monthly',
        bbl_per_mt: DEFAULT_BBL_PER_MT,
        gal_per_bbl: DEFAULT_GAL_PER_BBL,
        tradingview_symbol: 'NYMEX:HO',
        aliases: ['ULSD', 'HEATING OIL'],
        enabled: true
    })
});

const FIELD_OPTIONS = [
    { key: 'last', label: 'PX Last / Fair Value' },
    { key: 'close', label: 'PX Close' },
    { key: 'settle', label: 'PX Settle' },
    { key: 'fair', label: 'Fair Value' }
];
const FIELD_KEY_MAP = {
    last: 'PX_LAST',
    close: 'PX_CLOSE',
    settle: 'PX_SETTLE',
    fair: 'PX_FAIR_1430'
};

const PERIOD_LABELS = {
    Q1: 'Q1',
    Q2: 'Q2',
    Q3: 'Q3',
    Q4: 'Q4',
    'HALF 1': 'Half 1',
    'HALF 2': 'Half 2',
    H1: 'Half 1',
    H2: 'Half 2',
    '1H': 'Half 1',
    '2H': 'Half 2',
    'QUARTER 1': 'Q1',
    'QUARTER 2': 'Q2',
    'QUARTER 3': 'Q3',
    'QUARTER 4': 'Q4'
};
const MONTH_TO_QUARTER = {
    Jan: 'Q1', Feb: 'Q1', Mar: 'Q1',
    Apr: 'Q2', May: 'Q2', Jun: 'Q2',
    Jul: 'Q3', Aug: 'Q3', Sep: 'Q3',
    Oct: 'Q4', Nov: 'Q4', Dec: 'Q4'
};
const MONTH_TO_HALF = {
    Jan: 'Half 1', Feb: 'Half 1', Mar: 'Half 1',
    Apr: 'Half 1', May: 'Half 1', Jun: 'Half 1',
    Jul: 'Half 2', Aug: 'Half 2', Sep: 'Half 2',
    Oct: 'Half 2', Nov: 'Half 2', Dec: 'Half 2'
};

const COMMODITIES = Object.values(FALLBACK_ROOT_CONFIG).map((entry) => ({
    name: entry.name || entry.root,
    code: entry.root,
    rng: ''
}));

const MONTH_CODES = [
    { m: 'Jan', c: 'F', n: 1, q: 'Q1' },
    { m: 'Feb', c: 'G', n: 2, q: 'Q1' },
    { m: 'Mar', c: 'H', n: 3, q: 'Q1' },
    { m: 'Apr', c: 'J', n: 4, q: 'Q2' },
    { m: 'May', c: 'K', n: 5, q: 'Q2' },
    { m: 'Jun', c: 'M', n: 6, q: 'Q2' },
    { m: 'Jul', c: 'N', n: 7, q: 'Q3' },
    { m: 'Aug', c: 'Q', n: 8, q: 'Q3' },
    { m: 'Sep', c: 'U', n: 9, q: 'Q3' },
    { m: 'Oct', c: 'V', n: 10, q: 'Q4' },
    { m: 'Nov', c: 'X', n: 11, q: 'Q4' },
    { m: 'Dec', c: 'Z', n: 12, q: 'Q4' },
];

const MONTH_ABBR_MAP = {
    JAN: 'Jan',
    FEB: 'Feb',
    MAR: 'Mar',
    APR: 'Apr',
    MAY: 'May',
    JUN: 'Jun',
    JUL: 'Jul',
    AUG: 'Aug',
    SEP: 'Sep',
    OCT: 'Oct',
    NOV: 'Nov',
    DEC: 'Dec'
};

const MONTH_LETTER_MAP = {
    F: 'Jan',
    G: 'Feb',
    H: 'Mar',
    J: 'Apr',
    K: 'May',
    M: 'Jun',
    N: 'Jul',
    Q: 'Aug',
    U: 'Sep',
    V: 'Oct',
    X: 'Nov',
    Z: 'Dec'
};

const EXTENDED_MONTH_OPTIONS = [
    ...MONTH_CODES.map((month) => month.m),
    ...MONTH_CODES.map((month) => `${month.m} + 1`),
    'Q1',
    'Q2',
    'Q3',
    'Q4',
    'Half 1',
    'Half 2'
];

function readCssVar(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
}

function getThemePalette() {
    return {
        plotPaper: readCssVar('--plot-paper', '#f5f8fc'),
        plotBg: readCssVar('--plot-bg', '#f9fbff'),
        plotFont: readCssVar('--plot-font', '#334155'),
        plotMuted: readCssVar('--plot-muted', '#64748b'),
        plotGrid: readCssVar('--plot-grid', '#d7e2ef'),
        plotLine: readCssVar('--plot-line', '#94a3b8'),
        plotZero: readCssVar('--plot-zero', '#cbd5e1'),
        plotHoverBg: readCssVar('--plot-hover-bg', '#0f172a'),
        plotHoverBorder: readCssVar('--plot-hover-border', '#1e293b'),
        plotHoverFont: readCssVar('--plot-hover-font', '#f8fafc'),
        plotVolume: readCssVar('--plot-volume', 'rgba(37, 99, 235, 0.18)'),
        plotVarBar: readCssVar('--plot-var-bar', '#2563eb'),
        plotLegendBg: readCssVar('--plot-legend-bg', 'rgba(255,255,255,0)'),
        plotSeries: [
            readCssVar('--plot-series-1', '#2b6cb0'),
            readCssVar('--plot-series-2', '#22c55e'),
            readCssVar('--plot-series-3', '#f59e0b'),
            readCssVar('--plot-series-4', '#8b5cf6'),
            readCssVar('--plot-series-5', '#1f6feb'),
            readCssVar('--plot-series-6', '#e87979'),
            readCssVar('--plot-series-7', '#0ea5e9'),
            readCssVar('--plot-series-8', '#64748b')
        ]
    };
}

function getSeriesColors() {
    const palette = getThemePalette();
    return palette.plotSeries && palette.plotSeries.length ? palette.plotSeries : ['#2b6cb0', '#22c55e', '#f59e0b', '#8b5cf6', '#1f6feb', '#e87979', '#0ea5e9', '#64748b'];
}

const LEG_COUNT = 8;
const LEG_MODE_CONFIG = {
    single: { legs: 1, ratios: [1], showRatioRow: false, label: 'Single' },
    spread: { legs: 2, ratios: [1, -1], showRatioRow: false, label: 'Spread' },
    fly: { legs: 3, ratios: [1, -2, 1], showRatioRow: false, label: 'Fly' },
    box: { legs: 4, ratios: [1, -1, -1, 1], showRatioRow: false, label: 'Box' },
    multileg: { legs: 8, ratios: [0, 0, 0, 0, 0, 0, 0, 0], showRatioRow: true, label: 'Multi-Leg' }
};

const PREBUILT_SPREADS = [
    {
        id: 'ulsd_crack',
        label: 'ULSD Crack',
        formula: 'HO - next-month WTI',
        mode: 'multileg',
        unit: '$/bbl',
        legs: [
            { code: 'HO', ratio: 1, monthOffset: 0 },
            { code: 'CL', ratio: -1, monthOffset: 1 }
        ]
    },
    {
        id: 'jet_crack',
        label: 'USGC 54 Crack',
        formula: 'GC 54 Swap + next-month HO - next-month WTI',
        mode: 'multileg',
        unit: '$/bbl',
        legs: [
            { code: 'WU', ratio: 1, monthOffset: 0 },
            { code: 'HO', ratio: 1, monthOffset: 1 },
            { code: 'CL', ratio: -1, monthOffset: 1 }
        ]
    },
    {
        id: 'usgc_62_crack',
        label: 'USGC 62 Crack',
        formula: 'USGC 62 Swap + next-month HO - next-month WTI',
        mode: 'multileg',
        unit: '$/bbl',
        legs: [
            { code: 'UDS', ratio: 1, monthOffset: 0 },
            { code: 'HO', ratio: 1, monthOffset: 1 },
            { code: 'CL', ratio: -1, monthOffset: 1 }
        ]
    }
];

const legState = {
    mode: 'single',
    legs: Array.from({ length: LEG_COUNT }, () => ({
        code: '',
        month: '',
        ratio: 0
    }))
};

let lastStandardLegMode = 'single';
const workspaceLegDrafts = {
    spreads: null,
    multileg: null
};

let lastRenderedData = null;
let lastVolatilityHistogram = null;
let lastVarSeasonality = null;
let pendingChartUpdate = null;
let pendingPlotlyResize = null;
const SETTINGS_VERSION = 3;
let settingsSaveTimer = null;
const SHOULD_PERSIST_SETTINGS = window.location.protocol === 'file:';
const OWNER_UPDATE_API_ENABLED = window.DASHBOARD_OWNER_UPDATE_API !== false;
const UPDATE_API_STATUS_PATH = '/api/update/status';
const UPDATE_API_PATH = '/api/update';
const UPDATE_STATUS_POLL_MS = 1000;

function decodeBase64ToBytes(base64) {
    const chunkSize = 1000000;
    const chunks = [];
    let totalLength = 0;
    for (let i = 0; i < base64.length; i += chunkSize) {
        const chunk = base64.slice(i, i + chunkSize);
        const binary = atob(chunk);
        const bytes = new Uint8Array(binary.length);
        for (let j = 0; j < binary.length; j++) {
            bytes[j] = binary.charCodeAt(j);
        }
        totalLength += bytes.length;
        chunks.push(bytes);
    }
    const output = new Uint8Array(totalLength);
    let offset = 0;
    chunks.forEach((bytes) => {
        output.set(bytes, offset);
        offset += bytes.length;
    });
    return output;
}

function decodeEmbeddedFromDom() {
    if (window.EMBEDDED_DATA) return false;
    const rawEl = document.getElementById('embedded-data-raw');
    if (!rawEl) return false;
    const rawBase64 = rawEl.textContent.trim();
    if (!rawBase64) return false;
    try {
        const bytes = decodeBase64ToBytes(rawBase64);
        const rawText = new TextDecoder().decode(bytes);
        window.EMBEDDED_DATA = JSON.parse(rawText);
        if (!window.__EMBEDDED_READY__) {
            window.__EMBEDDED_READY__ = Promise.resolve();
        }
        return true;
    } catch (err) {
        console.error('Embedded data decode failed', err);
        return false;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const embeddedDecoded = decodeEmbeddedFromDom();
    const savedSettings = loadUserSettings();
    const initializeApp = () => {
        const years = getAvailableYears();
        state.years = getDefaultSelectedYears(years);

        const commodities = getCommodityList();
        if (commodities.length && !commodities.some(com => com.code === state.commodity)) {
            state.commodity = commodities[0].code;
        }

        initializeLegState();
        applyUserSettings(savedSettings);
        ensureDefaultLegSelection(savedSettings);
        renderYearGrid();
        renderMonthCodes();
        renderComList();
        renderLegGrid();
        bindLegModeButtons();
        bindSpreadModeControls();
        bindPrebuiltControls();
        bindSidebarToggles();
        bindFieldControls();
        bindDataUpdate();
        bindPersonalDownload();
        bindExportData();
        updateLastUpdated();
        bindVarHistogram();
        applyLegMode(legState.mode, { keepRatios: true, skipChart: true });
        syncUnitUI();
        updateSpreadModeUI();
        updateChart();
    };

    const waiters = [];
    const remoteReady = loadRemoteDataIfConfigured();
    if (remoteReady) {
        waiters.push(remoteReady);
    }

    const embeddedReady = window.__EMBEDDED_READY__;
    if (embeddedReady && typeof embeddedReady.then === 'function') {
        waiters.push(embeddedReady.then(() => {
            if (state.dataSource !== 'LAN') {
                updateDataStatus(window.EMBEDDED_DATA, 'Embedded');
            }
        }));
    } else if (embeddedDecoded) {
        updateDataStatus(window.EMBEDDED_DATA, 'Embedded');
    }

    if (waiters.length) {
        Promise.allSettled(waiters)
            .then(initializeApp)
            .catch((err) => {
                console.error('Data initialization failed', err);
                initializeApp();
            });
    } else {
        initializeApp();
    }

    scheduleRemotePolling();
});

// UI RENDERERS
function renderYearGrid() {
    const grid = document.getElementById('year-grid');
    grid.innerHTML = '';
    const years = getAvailableYears();
    if (!years.length) {
        for (let y = 2026; y >= 2000; y--) {
            years.push(y);
        }
    }

    for (let i = years.length - 1; i >= 0; i--) {
        const y = years[i];
        const isActive = state.years.includes(y);
        const div = document.createElement('div');
        div.className = `check-item ${isActive ? 'active' : ''}`;
        div.onclick = () => toggleYear(y);
        div.innerHTML = `
            <div class="check-box"><div class="check-mark"></div></div>
            <div class="check-label">${y}</div>
        `;
        grid.appendChild(div);
    }
}

function renderMonthCodes() {
    const list = document.getElementById('mc-list');
    list.innerHTML = '';
    MONTH_CODES.forEach(row => {
        const div = document.createElement('div');
        div.className = 'mc-row';
        div.innerHTML = `<div>${row.m}</div><div>${row.c}</div><div>${row.n}</div><div>${row.q}</div>`;
        list.appendChild(div);
    });
}

function renderComList() {
    const list = document.getElementById('com-list');
    list.innerHTML = '';
    const commodities = getSidebarCommodityList();
    const selectedRoots = getSelectedRootCodes();
    commodities.forEach((com) => {
        const div = document.createElement('div');
        const isActive =
            (selectedRoots.has(com.code)) ||
            (com.root_code && selectedRoots.has(com.root_code));
        div.className = `com-row ${isActive ? 'active' : ''}`;
        const rng = com.rng || com.contract_month_yr || '--';
        const codeLabel = com.root_code ? com.root_code : com.code;
        const rootConfig = getRootConfig(codeLabel);
        const nativeUnit = TRADE_MATH.normalizeUnit(
            com.native_unit || com.nativeUnit || (rootConfig && rootConfig.native_unit) || ''
        );
        const baseName = com.clean_name || com.name || com.code || codeLabel;
        const displayName = nativeUnit ? `${baseName} [${nativeUnit}]` : baseName;
        const productGroup = com.product_group || (rootConfig && rootConfig.product_group) || '';
        if (productGroup) {
            div.dataset.productGroup = productGroup;
            div.title = `${baseName} · ${productGroup}`;
        }
        div.innerHTML = `<div>${displayName}</div><div style="text-align:center">${codeLabel}</div><div style="text-align:right">${rng}</div>`;
        list.appendChild(div);
    });
}

function initializeLegState() {
    legState.legs.forEach((leg) => {
        leg.code = '';
        leg.month = '';
        leg.ratio = 0;
    });
    if (legState.legs[0].code) {
        syncCommodityFromLeg();
    }
    updateLegSummary();
}

function shouldAutoSelectLegs() {
    const embedded = window.EMBEDDED_DATA;
    if (embedded && embedded.meta && embedded.meta.auto_select_legs === false) {
        return false;
    }
    return false;
}

function ensureDefaultLegSelection(savedSettings) {
    if (savedSettings && Array.isArray(savedSettings.legs)) {
        return;
    }
    if (!shouldAutoSelectLegs()) {
        updateLegSummary();
        return;
    }
    const config = LEG_MODE_CONFIG[legState.mode] || LEG_MODE_CONFIG.single;
    const hasSelection = legState.legs.slice(0, config.legs).some((leg) => leg.code);
    if (hasSelection) return;
    const options = getRootCodeOptions();
    if (!options.length) return;
    legState.legs[0].code = options[0].value;
    syncCommodityFromLeg();
    updateLegSummary();
}

function renderLegGrid() {
    const grid = document.getElementById('leg-grid');
    if (!grid) return;
    grid.innerHTML = '';

    const rows = [
        { type: 'code', label: 'Code', className: 'leg-code', control: 'select' },
        { type: 'month', label: 'Month', className: 'leg-month', control: 'select' },
        { type: 'ratio', label: 'Ratio', className: 'leg-ratio', control: 'input', rowClass: 'ratio-row' }
    ];

    rows.forEach((row) => {
        const rowEl = document.createElement('div');
        rowEl.className = `leg-row ${row.rowClass || ''}`.trim();
        rowEl.dataset.rowType = row.type;

        for (let i = 1; i <= LEG_COUNT; i++) {
            const cell = document.createElement('div');
            cell.className = 'leg-cell';
            cell.dataset.leg = i.toString();

            const label = document.createElement('div');
            label.className = 'leg-label';
            const labelText = document.createElement('span');
            labelText.textContent = `${row.label} ${i}`;
            label.appendChild(labelText);
            if (row.type === 'code') {
                const resetBtn = document.createElement('button');
                resetBtn.type = 'button';
                resetBtn.className = 'leg-reset is-hidden';
                resetBtn.dataset.leg = i.toString();
                resetBtn.textContent = 'Reset';
                resetBtn.addEventListener('click', (event) => {
                    event.preventDefault();
                    resetLegSelection(i - 1);
                });
                label.appendChild(resetBtn);
            }
            cell.appendChild(label);

            if (row.control === 'select') {
                const select = document.createElement('select');
                select.className = `leg-select ${row.className}`;
                select.dataset.leg = i.toString();
                select.setAttribute('aria-label', `${row.label} ${i}`);
                cell.appendChild(select);
            } else {
                const input = document.createElement('input');
                input.type = 'number';
                input.step = '0.1';
                input.className = `leg-input ${row.className}`;
                input.dataset.leg = i.toString();
                input.setAttribute('aria-label', `${row.label} ${i}`);
                cell.appendChild(input);
            }

            if (row.type === 'code') {
                const badge = document.createElement('span');
                badge.className = 'leg-badge';
                badge.dataset.leg = i.toString();
                cell.appendChild(badge);
            }

            rowEl.appendChild(cell);
        }

        grid.appendChild(rowEl);
    });

    populateLegOptions();
    applyLegMode(legState.mode, {
        keepRatios: true,
        skipChart: true,
        fromPrebuilt: state.spreadMode === 'prebuilt'
    });
    updateLegFieldStates();
}

function populateLegOptions() {
    const codeOptions = getRootCodeOptions();

    document.querySelectorAll('.leg-code').forEach((select) => {
        const legIndex = Number(select.dataset.leg) - 1;
        select.innerHTML = '';
        const placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = 'Select';
        select.appendChild(placeholder);
        codeOptions.forEach((opt) => {
            const option = document.createElement('option');
            option.value = opt.value;
            option.textContent = opt.label;
            select.appendChild(option);
        });
        select.value = legState.legs[legIndex].code;
        select.addEventListener('change', (event) => {
            legState.legs[legIndex].code = event.target.value;
            if (isFlatCurveRoot(event.target.value)) {
                legState.legs[legIndex].month = '';
            }
            updateMonthOptionsForLeg(legIndex);
            if (legIndex === 0) {
                syncCommodityFromLeg();
            }
            updateLegSummary();
            updateLegFieldStates();
            updateChart();
            scheduleUserSettingsSave();
        });
    });

    document.querySelectorAll('.leg-month').forEach((select) => {
        const legIndex = Number(select.dataset.leg) - 1;
        const flatCurve = isFlatCurveRoot(legState.legs[legIndex].code);
        if (flatCurve) legState.legs[legIndex].month = '';
        const options = getMonthOptionsForRoot(legState.legs[legIndex].code);
        const normalized = normalizeMonthOptionValue(legState.legs[legIndex].month);
        populateMonthSelect(select, options, normalized);
        select.disabled = flatCurve;
        select.addEventListener('change', (event) => {
            legState.legs[legIndex].month = event.target.value;
            if (legIndex === 0) {
                syncCommodityFromLeg();
            }
            updateLegSummary();
            updateLegFieldStates();
            updateChart();
            scheduleUserSettingsSave();
        });
    });

    document.querySelectorAll('.leg-ratio').forEach((input) => {
        const legIndex = Number(input.dataset.leg) - 1;
        const ratioValue = legState.legs[legIndex].ratio;
        input.value = ratioValue === 0 ? '' : ratioValue;
        input.classList.toggle('is-nonzero', ratioValue !== 0);
        input.addEventListener('input', (event) => {
            const raw = event.target.value.trim();
            if (!raw) {
                legState.legs[legIndex].ratio = 0;
                event.target.classList.remove('is-nonzero');
            } else {
                const value = Number(raw);
                if (!Number.isNaN(value)) {
                    legState.legs[legIndex].ratio = value;
                    event.target.classList.toggle('is-nonzero', value !== 0);
                }
            }
            updateLegBadges();
            updateLegSummary();
            updateLegFieldStates();
            updateChart();
            scheduleUserSettingsSave();
        });
    });
}

function bindLegModeButtons() {
    document.querySelectorAll('.seg-btn').forEach((button) => {
        const mode = button.dataset.mode;
        if (!mode) return;
        button.addEventListener('click', () => {
            applyLegMode(mode, { skipChart: true });
            renderLegGrid();
            updateChart();
        });
    });
}

function bindVarHistogram() {
    const overlay = document.getElementById('var-overlay');
    const histogram = document.getElementById('var-histogram');
    if (!overlay || !histogram) return;
    overlay.addEventListener('mouseenter', () => {
        if (!state.showVar) return;
        histogram.classList.remove('is-hidden');
        renderVarHistogram(lastVolatilityHistogram);
    });
    overlay.addEventListener('mouseleave', () => {
        histogram.classList.add('is-hidden');
    });
}

function bindSpreadModeControls() {
    document.querySelectorAll('[data-spread-mode]').forEach((item) => {
        const mode = item.dataset.spreadMode;
        if (!mode) return;
        item.addEventListener('click', () => {
            setSpreadMode(mode);
        });
    });
    updateSpreadModeUI();
}

function bindPrebuiltControls() {
    const select = document.getElementById('prebuilt-select');
    if (!select) return;
    select.innerHTML = '';
    PREBUILT_SPREADS.forEach((spread) => {
        const option = document.createElement('option');
        option.value = spread.id;
        option.textContent = spread.label;
        select.appendChild(option);
    });
    state.prebuiltId = state.prebuiltId || (PREBUILT_SPREADS[0] ? PREBUILT_SPREADS[0].id : '');
    select.value = state.prebuiltId;
    updatePrebuiltSummary(PREBUILT_SPREADS.find((spread) => spread.id === state.prebuiltId));
    select.addEventListener('change', (event) => {
        state.prebuiltId = event.target.value;
        applyPrebuiltSpread();
        scheduleUserSettingsSave();
    });
    const monthSelect = document.getElementById('prebuilt-month');
    if (monthSelect) {
        const options = getMonthOptionsForRoot(legState.legs[0].code);
        const monthOptions = options.length ? options : EXTENDED_MONTH_OPTIONS.slice();
        populateMonthSelect(monthSelect, monthOptions, normalizeMonthOptionValue(legState.legs[0].month || ''));
        monthSelect.addEventListener('change', (event) => {
            const month = event.target.value;
            if (state.spreadMode === 'prebuilt') {
                applyPrebuiltSpread();
            } else {
                legState.legs.forEach((leg) => {
                    leg.month = month;
                });
                updateLegSummary();
                updateChart();
            }
            scheduleUserSettingsSave();
        });
    }
}

function bindSidebarToggles() {
    document.querySelectorAll('[data-toggle]').forEach((toggle) => {
        const key = toggle.dataset.toggle;
        if (!key) return;
        if (key === 'var') {
            toggle.classList.toggle('active', state.showVar);
        } else if (key === 'tradingview') {
            toggle.classList.remove('active');
            state.showTradingView = false;
        } else if (key === 'grayscale') {
            toggle.classList.toggle('active', state.grayscale);
        }
        toggle.addEventListener('click', () => {
            if (key === 'var') {
                toggle.classList.toggle('active');
                state.showVar = toggle.classList.contains('active');
                updateVarSeasonalityVisibility();
                scheduleChartUpdate();
                schedulePlotlyResize();
                scheduleUserSettingsSave();
            } else if (key === 'tradingview') {
                state.showTradingView = true;
                updateTradingViewVisibility();
            } else if (key === 'grayscale') {
                toggle.classList.toggle('active');
                state.grayscale = toggle.classList.contains('active');
                document.body.classList.toggle('is-grayscale', state.grayscale);
                scheduleUserSettingsSave();
            }
        });
    });
    updateVarSeasonalityVisibility();
    document.body.classList.toggle('is-grayscale', state.grayscale);
}

function bindFieldControls() {
    const availableOptions = getAvailableFieldOptions();
    const availableKeys = new Set(availableOptions.map((option) => option.key));
    if (!availableKeys.has(state.field)) {
        state.field = getDefaultFieldOptionKey(availableOptions);
    }
    const select = document.getElementById('field-select');
    if (select) {
        select.innerHTML = '';
        availableOptions.forEach((option) => {
            const item = document.createElement('option');
            item.value = option.key;
            item.textContent = option.label;
            select.appendChild(item);
        });
        select.addEventListener('change', () => {
            const key = select.value;
            if (key && key !== state.field) {
                setField(key);
            }
        });
    }
    document.querySelectorAll('[data-field]').forEach((button) => {
        const key = button.dataset.field;
        if (!key) return;
        const isAvailable = availableKeys.has(key);
        button.hidden = !isAvailable;
        button.setAttribute('aria-hidden', isAvailable ? 'false' : 'true');
        if (!isAvailable) return;
        button.addEventListener('click', () => {
            if (key === state.field) return;
            setField(key);
        });
    });
    syncFieldUI();
}

function getAvailableFieldOptions() {
    const available = window.EMBEDDED_DATA?.meta?.fields?.available;
    if (!Array.isArray(available) || !available.length) {
        return FIELD_OPTIONS.slice();
    }
    const availableBloombergKeys = new Set(
        available.map((field) => String(field || '').trim().toUpperCase()).filter(Boolean)
    );
    const options = FIELD_OPTIONS.filter((option) => (
        availableBloombergKeys.has(FIELD_KEY_MAP[option.key])
    ));
    return options.length ? options : FIELD_OPTIONS.slice(0, 1);
}

function getDefaultFieldOptionKey(options = getAvailableFieldOptions()) {
    const defaultBloombergKey = getDefaultFieldKey();
    const matching = options.find((option) => FIELD_KEY_MAP[option.key] === defaultBloombergKey);
    return (matching || options[0] || FIELD_OPTIONS[0]).key;
}

function scheduleChartUpdate() {
    if (pendingChartUpdate) {
        cancelAnimationFrame(pendingChartUpdate);
    }
    pendingChartUpdate = requestAnimationFrame(() => {
        pendingChartUpdate = null;
        updateChart();
    });
}

function isHttpDashboard() {
    return window.location.protocol === 'http:' || window.location.protocol === 'https:';
}

function setDataUpdateState(kind, message) {
    const button = document.getElementById('data-update');
    const status = document.getElementById('data-update-status');
    const stateKind = ['loading', 'success', 'error', 'unavailable'].includes(kind) ? kind : '';
    const statusKind = stateKind === 'unavailable' ? 'error' : stateKind;

    if (button) {
        button.disabled = stateKind === 'loading' || stateKind === 'success' || stateKind === 'unavailable';
        button.textContent = stateKind === 'loading' ? 'UPDATING…' : 'UPDATE DATA';
        button.setAttribute('aria-busy', stateKind === 'loading' ? 'true' : 'false');
    }
    if (status) {
        status.classList.remove('is-loading', 'is-success', 'is-error');
        if (statusKind) status.classList.add(`is-${statusKind}`);
        status.textContent = message || '';
    }
}

async function readDataUpdateResponse(response) {
    const text = await response.text();
    if (!text) return {};
    try {
        return JSON.parse(text);
    } catch (err) {
        return { message: text };
    }
}

function dataUpdateMessage(payload, fallback) {
    if (!payload || typeof payload !== 'object') return fallback;
    const detail = payload.error || payload.detail || payload.message;
    return typeof detail === 'string' && detail.trim() ? detail.trim() : fallback;
}

function reloadDashboardAfterUpdate() {
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set('_updated', String(Date.now()));
    window.location.replace(nextUrl.toString());
}

function waitForUpdatePoll() {
    return new Promise((resolve) => window.setTimeout(resolve, UPDATE_STATUS_POLL_MS));
}

async function waitForDataUpdateCompletion() {
    let failedPolls = 0;
    while (true) {
        await waitForUpdatePoll();
        try {
            const response = await fetch(UPDATE_API_STATUS_PATH, {
                method: 'GET',
                headers: { Accept: 'application/json' },
                cache: 'no-store'
            });
            if (!response.ok) throw new Error(`Update status failed (${response.status}).`);
            const payload = await readDataUpdateResponse(response);
            failedPolls = 0;
            if (payload.updating === true || payload.running === true) {
                setDataUpdateState('loading', dataUpdateMessage(payload, 'Updating Bloomberg data…'));
                continue;
            }
            if (payload.last_success === true) return payload.last_result || payload;
            if (payload.last_success === false) {
                throw new Error(dataUpdateMessage(payload, 'Bloomberg update failed.'));
            }
            throw new Error('The local update server lost the update result.');
        } catch (err) {
            if (err && err.message && !err.message.startsWith('Failed to fetch')) throw err;
            failedPolls += 1;
            if (failedPolls >= 3) {
                throw new Error('Could not reach the local update server. Keep UPDATE_AND_OPEN.bat running and retry.');
            }
        }
    }
}

async function probeDataUpdateApi() {
    const button = document.getElementById('data-update');
    if (!OWNER_UPDATE_API_ENABLED || !button || !isHttpDashboard()) return false;

    try {
        const response = await fetch(UPDATE_API_STATUS_PATH, {
            method: 'GET',
            headers: { Accept: 'application/json' },
            cache: 'no-store'
        });
        if (!response.ok) return false;
        const payload = await readDataUpdateResponse(response);
        if (!payload || payload.update_api !== true) return false;

        button.classList.remove('is-hidden');
        if (payload.available === false) {
            setDataUpdateState(
                'unavailable',
                dataUpdateMessage(payload, 'Bloomberg updating is unavailable on this machine.')
            );
            return true;
        }
        const updating = payload.updating === true || payload.running === true;
        const readyMessage = payload.config ? `Update source: ${payload.config}` : '';
        setDataUpdateState(updating ? 'loading' : '', updating ? 'An update is already in progress…' : readyMessage);
        return true;
    } catch (err) {
        return false;
    }
}

function bindDataUpdate() {
    const button = document.getElementById('data-update');
    if (!OWNER_UPDATE_API_ENABLED || !button || !isHttpDashboard()) return;

    button.addEventListener('click', async () => {
        setDataUpdateState('loading', 'Updating Bloomberg data and rebuilding the export…');
        try {
            const response = await fetch(UPDATE_API_PATH, {
                method: 'POST',
                headers: { Accept: 'application/json' },
                cache: 'no-store'
            });
            const payload = await readDataUpdateResponse(response);
            if (!response.ok || payload.ok === false || payload.success === false) {
                throw new Error(dataUpdateMessage(payload, `Update request failed (${response.status}).`));
            }
            const completed = payload.accepted === true || payload.updating === true
                ? await waitForDataUpdateCompletion()
                : payload;
            setDataUpdateState('success', dataUpdateMessage(completed, 'Update complete. Reloading…'));
            window.setTimeout(reloadDashboardAfterUpdate, 400);
        } catch (err) {
            const message = err && err.message === 'Failed to fetch'
                ? 'Could not reach the local update server. Keep UPDATE_AND_OPEN.bat running and retry.'
                : (err && err.message ? err.message : 'The update could not be completed.');
            setDataUpdateState('error', `Update failed: ${message}`);
        }
    });

    void probeDataUpdateApi();
}

function bindPersonalDownload() {
    const button = document.getElementById('download-personal');
    if (!button) return;
    button.addEventListener('click', () => {
        downloadPersonalCopy();
    });
}

function bindExportData() {
    const button = document.getElementById('export-data');
    if (!button) return;
    button.addEventListener('click', () => {
        exportTradingData();
    });
}

function csvValue(value) {
    if (value == null) return '';
    const text = String(value);
    if (/[",\n]/.test(text)) {
        return `"${text.replace(/"/g, '""')}"`;
    }
    return text;
}

function buildCsv(rows) {
    return rows.map((row) => row.map(csvValue).join(',')).join('\n');
}

function downloadCsv(content, filename) {
    const blob = new Blob([content], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}

function sanitizeFilename(value) {
    return String(value || 'export')
        .trim()
        .replace(/\s+/g, '_')
        .replace(/[^a-zA-Z0-9_.-]/g, '');
}

function buildMonthDayLabels(values) {
    if (!Array.isArray(values)) return [];
    const baseYear = 2000;
    return values.map((value) => {
        if (!Number.isFinite(value)) return '';
        const day = Math.round(value);
        const date = new Date(baseYear, 0, 1);
        date.setDate(day);
        return date.toLocaleDateString(undefined, { month: 'short', day: '2-digit' });
    });
}

function collectSeriesX(seriesMap) {
    const set = new Set();
    if (!seriesMap) return [];
    Object.values(seriesMap).forEach((series) => {
        if (!series || !Array.isArray(series.x)) return;
        series.x.forEach((value) => {
            if (Number.isFinite(value)) {
                set.add(Number(value));
            }
        });
    });
    return Array.from(set).sort((a, b) => a - b);
}

function buildSeriesLookup(series) {
    const map = new Map();
    if (!series || !Array.isArray(series.x) || !Array.isArray(series.y)) return map;
    const length = Math.min(series.x.length, series.y.length);
    for (let i = 0; i < length; i++) {
        const x = series.x[i];
        const y = series.y[i];
        if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
        map.set(String(x), y);
    }
    return map;
}

function seriesHasValues(series) {
    if (!series) return false;
    const values = Array.isArray(series) ? series : series.y;
    if (!Array.isArray(values)) return false;
    return values.some((value) => Number.isFinite(value));
}

function seriesMapHasValues(seriesMap) {
    if (!seriesMap) return false;
    return Object.values(seriesMap).some((series) => seriesHasValues(series));
}

function appendPivotSection(rows, title, unit, xValues, columns, seriesMap) {
    rows.push([unit ? `${title} (${unit})` : title]);
    rows.push(['Date', ...columns.map(String)]);
    const labels = buildMonthDayLabels(xValues);
    const lookups = columns.reduce((acc, col) => {
        const series = seriesMap[col] || seriesMap[String(col)];
        acc[col] = buildSeriesLookup(series);
        return acc;
    }, {});
    xValues.forEach((x, index) => {
        const row = [labels[index] || ''];
        columns.forEach((col) => {
            const value = lookups[col] ? lookups[col].get(String(x)) : null;
            row.push(Number.isFinite(value) ? TRADE_MATH.round5(value) : '');
        });
        rows.push(row);
    });
    rows.push([]);
}

function exportTradingData() {
    if (!lastRenderedData || !lastRenderedData.series) {
        alert('No chart data to export.');
        return;
    }
    const rows = [];
    const formula = getLegFormulaLabel() || 'Series';
    const unit = state.unit || '';
    const years = Object.keys(lastRenderedData.series).map(Number).sort((a, b) => a - b);
    const mainYears = resolveActiveYears(years);
    const mainX = collectSeriesX(lastRenderedData.series);
    appendPivotSection(rows, formula, unit, mainX, mainYears, lastRenderedData.series);

    if (state.showVar && lastVarSeasonality && lastVarSeasonality.series) {
        const varYears = Object.keys(lastVarSeasonality.series).map(Number).sort((a, b) => a - b);
        const filteredVarYears = resolveActiveYears(varYears);
        const varX = collectSeriesX(lastVarSeasonality.series);
        appendPivotSection(rows, 'VaR Seasonality', unit, varX, filteredVarYears, lastVarSeasonality.series);
    }


    if (state.showVar && lastVolatilityHistogram && Array.isArray(lastVolatilityHistogram.bins)) {
        rows.push(['VaR Histogram (% Change)']);
        rows.push(['Bin', 'Count']);
        lastVolatilityHistogram.bins.forEach((bin, index) => {
            const value = lastVolatilityHistogram.values ? lastVolatilityHistogram.values[index] : null;
            if (!Number.isFinite(value)) return;
            rows.push([bin, value]);
        });
        rows.push([]);
    }

    const filename = sanitizeFilename(`Pricing Dashboard Trade Builder ${formula}`) + '.csv';
    downloadCsv(buildCsv(rows), filename);
}

function getSettingsKey() {
    return `pricing_dashboard_trade_builder_settings_v${SETTINGS_VERSION}`;
}

function loadUserSettings() {
    if (!SHOULD_PERSIST_SETTINGS) return null;
    try {
        const stored = localStorage.getItem(getSettingsKey());
        if (!stored) return null;
        return JSON.parse(stored);
    } catch (err) {
        console.warn('Failed to read saved settings', err);
        return null;
    }
}

function applyUserSettings(saved) {
    if (!saved) return;
    if (Array.isArray(saved.years)) {
        state.years = saved.years.filter((value) => Number.isFinite(Number(value))).map(Number);
    }
    if (typeof saved.unit === 'string') {
        state.unit = saved.unit;
    }
    if (typeof saved.field === 'string') {
        const allowed = new Set(FIELD_OPTIONS.map((option) => option.key));
        state.field = allowed.has(saved.field) ? saved.field : 'last';
    }
    if (typeof saved.commodity === 'string') {
        state.commodity = saved.commodity;
    }
    if (typeof saved.spreadMode === 'string') {
        if (saved.spreadMode === 'standard') {
            state.spreadMode = 'spreads';
        } else if (saved.spreadMode === 'custom') {
            state.spreadMode = 'multileg';
        } else {
            state.spreadMode = ['spreads', 'prebuilt', 'multileg'].includes(saved.spreadMode)
                ? saved.spreadMode
                : 'spreads';
        }
    }
    if (typeof saved.customSpreadName === 'string') {
        state.customSpreadName = saved.customSpreadName;
    }
    if (typeof saved.prebuiltId === 'string') {
        state.prebuiltId = saved.prebuiltId;
    }
    if (typeof saved.showVar === 'boolean') {
        state.showVar = saved.showVar;
    }
    state.showTradingView = false;
    if (typeof saved.grayscale === 'boolean') {
        state.grayscale = saved.grayscale;
    }
    if (saved.legState && Array.isArray(saved.legState.legs)) {
        legState.mode = saved.legState.mode || legState.mode;
        legState.legs = saved.legState.legs.map((leg, index) => ({
            code: typeof leg.code === 'string' ? leg.code : legState.legs[index].code,
            month: typeof leg.month === 'string' ? leg.month : legState.legs[index].month,
            ratio: Number.isFinite(Number(leg.ratio)) ? Number(leg.ratio) : legState.legs[index].ratio
        }));
        if (['single', 'spread', 'fly', 'box'].includes(legState.mode)) {
            lastStandardLegMode = legState.mode;
        }
    }
}

function saveUserSettings() {
    if (!SHOULD_PERSIST_SETTINGS) return;
    try {
        const payload = {
            years: state.years,
            unit: state.unit,
            field: state.field,
            commodity: state.commodity,
            spreadMode: state.spreadMode,
            customSpreadName: state.customSpreadName,
            prebuiltId: state.prebuiltId,
            showVar: state.showVar,
            showTradingView: state.showTradingView,
            grayscale: state.grayscale,
            legState: {
                mode: legState.mode,
                legs: legState.legs
            }
        };
        localStorage.setItem(getSettingsKey(), JSON.stringify(payload));
    } catch (err) {
        console.warn('Failed to save settings', err);
    }
}

function scheduleUserSettingsSave() {
    if (!SHOULD_PERSIST_SETTINGS) return;
    if (settingsSaveTimer) {
        clearTimeout(settingsSaveTimer);
    }
    settingsSaveTimer = setTimeout(saveUserSettings, 250);
}

function getRemoteDataUrl() {
    const params = new URLSearchParams(window.location.search);
    const paramUrl = params.get('data');
    const configUrl = window.DASHBOARD_DATA_URL;
    return paramUrl || configUrl || '';
}

function getDataUpdatedAt(data) {
    if (!data || !data.meta) return null;
    const dataDate = String(data.meta.data_max_date || data.meta.dataMaxDate || '').trim();
    const dateOnlyMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dataDate);
    if (dateOnlyMatch) {
        const parsedLocalDate = new Date(
            Number(dateOnlyMatch[1]),
            Number(dateOnlyMatch[2]) - 1,
            Number(dateOnlyMatch[3])
        );
        if (!Number.isNaN(parsedLocalDate.getTime())) return parsedLocalDate;
    }
    const stamp = data.meta.updated_at || data.meta.updatedAt || data.meta.last_updated;
    if (!stamp) return null;
    const parsed = new Date(stamp);
    if (Number.isNaN(parsed.getTime())) return null;
    return parsed;
}

function updateDataStatus(data, sourceLabel) {
    const sourceEl = document.getElementById('data-source');

    if (sourceEl) {
        sourceEl.textContent = `Data: ${sourceLabel}`;
    }

    const updatedAt = getDataUpdatedAt(data);
    state.dataUpdatedAt = updatedAt;
    updateLastUpdatedFromData(data);
}

async function fetchRemoteData(url) {
    const response = await fetch(url, { cache: 'no-store' });
    if (!response.ok) {
        throw new Error(`Remote data request failed: ${response.status}`);
    }
    const contentType = response.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
        return response.json();
    }
    const text = await response.text();
    return JSON.parse(text);
}

async function loadRemoteDataIfConfigured() {
    const url = getRemoteDataUrl();
    if (!url) return null;
    try {
        const data = await fetchRemoteData(url);
        if (data && data.commodities && data.meta) {
            window.EMBEDDED_DATA = data;
            state.dataSource = 'LAN';
            updateDataStatus(data, 'LAN');
            updateLastUpdatedFromData(data);
            return true;
        }
        return false;
    } catch (err) {
        console.warn('Remote data load failed, falling back to embedded data.', err);
        return false;
    }
}

function scheduleRemotePolling() {
    const url = getRemoteDataUrl();
    const pollMs = Number(window.DASHBOARD_DATA_POLL_MS || 0);
    if (!url || !pollMs || Number.isNaN(pollMs)) return;
    setInterval(async () => {
        const updated = await loadRemoteDataIfConfigured();
        if (updated) {
            updateChart();
        }
    }, pollMs);
}

function updateLastUpdatedFromData(data) {
    if (!data || !data.meta) return false;
    const parsed = getDataUpdatedAt(data);
    if (!parsed) return false;
    const formatted = parsed.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: '2-digit' });
    const el = document.getElementById('data-last-update');
    if (el) {
        el.textContent = `Last update: ${formatted}`;
    }
    return true;
}

function updateVarSeasonalityVisibility() {
    const section = document.getElementById('var-seasonality-section');
    if (!section) return;
    section.classList.toggle('is-hidden', !state.showVar);
}

function updateTradingViewVisibility() {
    if (!state.showTradingView) return;
    openTradingViewExternal();
}

function resizePlotOnNextFrame(container) {
    if (!container || !container.data) return;
    requestAnimationFrame(() => {
        if (!container || container.offsetParent === null) return;
        Plotly.Plots.resize(container);
    });
}

function getPlotlyContainers() {
    return [
        document.getElementById('plotly-div'),
        document.getElementById('var-seasonality-plot'),
        document.getElementById('volume-seasonality-plot'),
        document.getElementById('var-histogram-plot')
    ];
}

function resizeVisiblePlotlyCharts() {
    if (!window.Plotly || !Plotly.Plots || typeof Plotly.Plots.resize !== 'function') return;
    getPlotlyContainers().forEach((container) => {
        if (!container || !container.data) return;
        if (container.offsetParent === null) return;
        Plotly.Plots.resize(container);
    });
}

function schedulePlotlyResize() {
    if (pendingPlotlyResize) {
        cancelAnimationFrame(pendingPlotlyResize);
    }
    pendingPlotlyResize = requestAnimationFrame(() => {
        pendingPlotlyResize = null;
        resizeVisiblePlotlyCharts();
        setTimeout(resizeVisiblePlotlyCharts, 120);
    });
}


function setSpreadMode(mode) {
    if (!mode || mode === state.spreadMode) return;
    const previousMode = state.spreadMode;
    const transitionDraft = captureLegDraft();
    if (previousMode === 'spreads' || previousMode === 'multileg') {
        workspaceLegDrafts[previousMode] = transitionDraft;
    }
    state.spreadMode = mode;
    updateSpreadModeUI();

    if (mode === 'multileg') {
        if (!restoreLegDraft(workspaceLegDrafts.multileg)) {
            restoreLegDraft(transitionDraft);
            applyLegMode('multileg', { keepRatios: true, skipChart: true });
        }
        renderLegGrid();
        updateChart();
    } else if (mode === 'spreads') {
        if (!restoreLegDraft(workspaceLegDrafts.spreads)) {
            restoreLegDraft(transitionDraft);
            applyLegMode(lastStandardLegMode, { skipChart: true });
        }
        renderLegGrid();
        updateChart();
    } else if (mode === 'prebuilt') {
        applyPrebuiltSpread();
    }
    scheduleUserSettingsSave();
}

function updateSpreadModeUI() {
    document.querySelectorAll('[data-spread-mode]').forEach((item) => {
        item.classList.toggle('active', item.dataset.spreadMode === state.spreadMode);
    });
    const controls = document.getElementById('prebuilt-controls');
    if (controls) {
        controls.classList.toggle('is-hidden', state.spreadMode !== 'prebuilt');
    }
    const segmented = document.querySelector('.segmented');
    if (segmented) {
        segmented.classList.toggle('is-hidden', state.spreadMode !== 'spreads');
    }
    const legGrid = document.getElementById('leg-grid');
    if (legGrid) {
        legGrid.classList.toggle('is-hidden', state.spreadMode === 'prebuilt');
    }
}

function applyPrebuiltSpread() {
    const current = PREBUILT_SPREADS.find((spread) => spread.id === state.prebuiltId) || PREBUILT_SPREADS[0];
    if (!current) return;

    const monthSelect = document.getElementById('prebuilt-month');
    const month = (monthSelect && monthSelect.value) || legState.legs[0].month || '';

    state.unit = current.unit || '$/bbl';
    syncUnitUI();

    resetLegSelections({ clearRatios: true });
    if (current.legs && current.legs.length) {
        const inferredMode = ({ 1: 'single', 2: 'spread', 3: 'fly', 4: 'box' })[current.legs.length] || 'multileg';
        applyLegMode(current.mode || inferredMode, { fromPrebuilt: true, skipChart: true, keepRatios: true });
        legState.legs.forEach((leg, index) => {
            const def = current.legs[index];
            if (!def) return;
            leg.code = resolvePrebuiltCode(def.code);
            leg.ratio = def.ratio;
            leg.month = isFlatCurveRoot(leg.code)
                ? ''
                : (def.monthOffset ? getOffsetMonth(month, def.monthOffset) : month);
        });
    }

    renderLegGrid();
    if (monthSelect) {
        monthSelect.value = normalizeMonthOptionValue(month);
    }
    const missing = getMissingLegCodes(getLegsForCalculation());
    const warning = missing.length ? `Missing data for ${missing.join(', ')}` : '';
    updatePrebuiltSummary(current, warning);
    syncCommodityFromLeg();
    updateLegSummary();
    updateChart();
    scheduleUserSettingsSave();
}

function updateLastUpdated() {
    const el = document.getElementById('data-last-update');
    if (!el) return;
    if (updateLastUpdatedFromData(window.EMBEDDED_DATA)) return;
    const stamp = document.lastModified ? new Date(document.lastModified) : new Date();
    if (!Number.isNaN(stamp.getTime())) {
        const formatted = stamp.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: '2-digit' });
        el.textContent = `Last update: ${formatted}`;
    }
}

function downloadPersonalCopy() {
    const embedded = window.EMBEDDED_DATA;
    if (embedded && embedded.commodities && embedded.meta) {
        const configBlock = buildConfigBlock('', 0);
        let html = buildPersonalHtml(configBlock);
        html = injectEmbeddedData(html, embedded);
        triggerDownload(html, 'Pricing Dashboard - Trade Builder.html');
        return;
    }

    const remoteUrl = getRemoteDataUrl();
    if (!remoteUrl) {
        alert('Embedded data is unavailable. Configure a LAN data URL or use the export script.');
        return;
    }

    const pollMs = Number(window.DASHBOARD_DATA_POLL_MS || 60000);
    const configBlock = buildConfigBlock(remoteUrl, Number.isNaN(pollMs) ? 60000 : pollMs);
    const html = buildPersonalHtml(configBlock);
    triggerDownload(html, 'Pricing Dashboard - Trade Builder.html');
}

function buildConfigBlock(remoteUrl, pollMs) {
    const urlValue = JSON.stringify(remoteUrl);
    return (
        '<script>\n' +
        `  window.DASHBOARD_DATA_URL = ${urlValue};\n` +
        `  window.DASHBOARD_DATA_POLL_MS = ${pollMs};\n` +
        '  window.DASHBOARD_OWNER_UPDATE_API = false;\n' +
        '</' + 'script>'
    );
}

function buildPersonalHtml(configBlock) {
    const html = '<!DOCTYPE html>\n' + document.documentElement.outerHTML;
    const regex = new RegExp('<script>\\s*window\\.DASHBOARD_DATA_URL[\\s\\S]*?<\\/' + 'script>');
    if (regex.test(html)) {
        return html.replace(regex, configBlock);
    }
    return html.replace('</head>', `${configBlock}\n</head>`);
}

function injectEmbeddedData(html, data) {
    if (!data) return html;
    if (html.includes('embedded-data-raw') || html.includes('window.EMBEDDED_DATA')) {
        return html;
    }
    const payload = JSON.stringify(data).replace(/</g, '\\u003c');
    const block = (
        '<script>\n' +
        `  window.EMBEDDED_DATA = ${payload};\n` +
        '  window.__EMBEDDED_READY__ = Promise.resolve();\n' +
        '</' + 'script>'
    );
    return html.replace('</head>', `${block}\n</head>`);
}

function triggerDownload(content, filename) {
    const blob = new Blob([content], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
}

function applyLegMode(mode, options = {}) {
    const config = LEG_MODE_CONFIG[mode] || LEG_MODE_CONFIG.single;
    legState.mode = mode;
    if (['single', 'spread', 'fly', 'box'].includes(mode)) {
        lastStandardLegMode = mode;
    }
    const applyDefaults = !options.keepRatios;

    legState.legs.forEach((leg, index) => {
        if (applyDefaults && typeof config.ratios[index] === 'number') {
            leg.ratio = config.ratios[index];
        }
        if (!applyDefaults && typeof leg.ratio !== 'number') {
            leg.ratio = 0;
        }
    });

    if (!options.fromPrebuilt && state.spreadMode === 'prebuilt') {
        state.spreadMode = 'spreads';
        updateSpreadModeUI();
    }

    document.querySelectorAll('.seg-btn').forEach((button) => {
        button.classList.toggle('active', button.dataset.mode === mode);
    });

    document.querySelectorAll('.leg-ratio').forEach((input) => {
        const legIndex = Number(input.dataset.leg) - 1;
        const ratioValue = legState.legs[legIndex].ratio;
        input.value = ratioValue === 0 ? '' : ratioValue;
        input.classList.toggle('is-nonzero', ratioValue !== 0);
    });

    updateLegVisibility();
    updateLegBadges();
    updateLegSummary();
    if (!options.skipChart) {
        updateChart();
    }
    scheduleUserSettingsSave();
}

function updateLegVisibility() {
    const config = LEG_MODE_CONFIG[legState.mode];
    document.querySelectorAll('.leg-cell').forEach((cell) => {
        const legIndex = Number(cell.dataset.leg);
        const hide = legIndex > config.legs;
        cell.classList.toggle('is-hidden', hide);
    });

    document.querySelectorAll('.ratio-row').forEach((row) => {
        row.classList.toggle('is-hidden', !config.showRatioRow);
    });
}

function getEffectiveLegRatio(leg, index) {
    const config = LEG_MODE_CONFIG[legState.mode] || LEG_MODE_CONFIG.single;
    if (state.spreadMode === 'spreads' && !config.showRatioRow && typeof config.ratios[index] === 'number') {
        return config.ratios[index];
    }
    const ratio = Number(leg.ratio);
    return Number.isFinite(ratio) ? ratio : 0;
}

function updateLegBadges() {
    const config = LEG_MODE_CONFIG[legState.mode];
    document.querySelectorAll('.leg-badge').forEach((badge) => {
        const legIndex = Number(badge.dataset.leg) - 1;
        const ratio = getEffectiveLegRatio(legState.legs[legIndex], legIndex);
        const text = ratio > 0 ? `+${ratio}` : `${ratio}`;
        badge.textContent = text;
        badge.classList.toggle('is-active', ratio !== 0);
        badge.classList.toggle('is-hidden', config.showRatioRow);
    });
    updateLegFieldStates();
}

function updateLegFieldStates() {
    const config = LEG_MODE_CONFIG[legState.mode] || LEG_MODE_CONFIG.single;
    const showReset = state.spreadMode === 'multileg' && legState.mode === 'multileg';
    const activeLegs = legState.legs.slice(0, config.legs);
    const allActiveLegsAreFlat = activeLegs.length > 0 && activeLegs.every((leg) => (
        Boolean(leg && leg.code) && isFlatCurveRoot(leg.code)
    ));

    document.querySelectorAll('.leg-row[data-row-type="month"]').forEach((row) => {
        row.classList.toggle('is-hidden', allActiveLegsAreFlat);
        row.setAttribute('aria-hidden', allActiveLegsAreFlat ? 'true' : 'false');
    });

    document.querySelectorAll('.leg-reset').forEach((btn) => {
        const legIndex = Number(btn.dataset.leg);
        const hide = !showReset || legIndex > config.legs;
        btn.classList.toggle('is-hidden', hide);
    });

    for (let i = 0; i < LEG_COUNT; i++) {
        const legIndex = i + 1;
        const codeEl = document.querySelector(`.leg-code[data-leg="${legIndex}"]`);
        const monthEl = document.querySelector(`.leg-month[data-leg="${legIndex}"]`);
        const ratioEl = document.querySelector(`.leg-ratio[data-leg="${legIndex}"]`);

        [codeEl, monthEl, ratioEl].forEach((el) => {
            if (!el) return;
            el.classList.remove('is-active', 'is-missing');
        });

        const activeLeg = legIndex <= config.legs;
        const leg = legState.legs[i] || {};
        const codeVal = leg.code;
        const flatCurve = activeLeg && Boolean(codeVal) && isFlatCurveRoot(codeVal);
        const monthCell = monthEl ? monthEl.closest('.leg-cell') : null;
        if (monthCell) {
            monthCell.classList.toggle('is-hidden', !activeLeg);
            monthCell.classList.toggle('is-monthless-placeholder', flatCurve);
            monthCell.setAttribute('aria-hidden', flatCurve || !activeLeg ? 'true' : 'false');
        }
        if (monthEl) {
            monthEl.disabled = flatCurve;
            monthEl.setAttribute('aria-hidden', flatCurve ? 'true' : 'false');
        }
        if (!activeLeg) continue;
        if (flatCurve && leg.month) {
            leg.month = '';
            if (monthEl) monthEl.value = '';
        }
        const monthVal = leg.month;
        const ratioVal = Number(leg.ratio);
        const ratioHas = Number.isFinite(ratioVal) && ratioVal !== 0;
        const hasAny = Boolean(codeVal || monthVal || ratioHas);
        if (!hasAny) continue;

        if (codeEl) {
            codeEl.classList.add(codeVal ? 'is-active' : 'is-missing');
        }
        if (monthEl && !flatCurve) {
            monthEl.classList.add(monthVal ? 'is-active' : 'is-missing');
        }
        if (ratioEl && config.showRatioRow) {
            ratioEl.classList.add(ratioHas ? 'is-active' : 'is-missing');
        }
    }
}

function resetLegSelections(options = {}) {
    const clearRatios = options.clearRatios !== false;
    legState.legs.forEach((leg) => {
        leg.code = '';
        leg.month = '';
        if (clearRatios) {
            leg.ratio = 0;
        }
    });
}

function captureLegDraft() {
    return {
        mode: legState.mode,
        legs: legState.legs.map((leg, index) => ({
            code: leg.code || '',
            month: leg.month || '',
            ratio: getEffectiveLegRatio(leg, index)
        }))
    };
}

function restoreLegDraft(draft) {
    if (!draft || !Array.isArray(draft.legs)) return false;
    const mode = LEG_MODE_CONFIG[draft.mode] ? draft.mode : 'single';
    legState.mode = mode;
    for (let index = 0; index < LEG_COUNT; index++) {
        const saved = draft.legs[index] || {};
        const target = legState.legs[index] || (legState.legs[index] = { code: '', month: '', ratio: 0 });
        target.code = typeof saved.code === 'string' ? saved.code : '';
        target.month = isFlatCurveRoot(target.code)
            ? ''
            : (typeof saved.month === 'string' ? saved.month : '');
        target.ratio = Number.isFinite(Number(saved.ratio)) ? Number(saved.ratio) : 0;
    }
    if (['single', 'spread', 'fly', 'box'].includes(mode)) {
        lastStandardLegMode = mode;
    }
    return true;
}

function resetLegSelection(index) {
    if (state.spreadMode !== 'multileg' || legState.mode !== 'multileg') return;
    if (index < 0 || index >= legState.legs.length) return;
    legState.legs[index].code = '';
    legState.legs[index].month = '';
    legState.legs[index].ratio = 0;

    const legNumber = index + 1;
    const codeEl = document.querySelector(`.leg-code[data-leg="${legNumber}"]`);
    const monthEl = document.querySelector(`.leg-month[data-leg="${legNumber}"]`);
    const ratioEl = document.querySelector(`.leg-ratio[data-leg="${legNumber}"]`);
    if (codeEl) codeEl.value = '';
    if (monthEl) monthEl.value = '';
    if (ratioEl) {
        ratioEl.value = '';
        ratioEl.classList.remove('is-nonzero');
    }
    if (index === 0) {
        syncCommodityFromLeg();
    }
    updateLegBadges();
    updateLegSummary();
    updateLegFieldStates();
    updateChart();
    scheduleUserSettingsSave();
}

function getActiveLegs() {
    const config = LEG_MODE_CONFIG[legState.mode] || LEG_MODE_CONFIG.single;
    return legState.legs.slice(0, config.legs);
}

function getLegsWithEffectiveRatios() {
    return getActiveLegs().map((leg, index) => ({
        ...leg,
        ratio: getEffectiveLegRatio(leg, index)
    }));
}

function getLegsForDisplay() {
    return getLegsWithEffectiveRatios().filter((leg) => leg.code);
}

function buildLegFormula(legs) {
    const includeMonth = arguments.length > 1 && arguments[1] && arguments[1].includeMonth;
    const parts = [];
    legs.forEach((leg) => {
        const ratio = Number(leg.ratio);
        if (!Number.isFinite(ratio) || ratio === 0) return;
        const resolved = resolveCommodityForLeg(leg);
        let codeLabel = canonicalizeRootCode(
            (resolved && (resolved.root_code || resolved.security)) || leg.code || (resolved && resolved.code)
        );
        if (includeMonth && leg && leg.month) {
            const monthLabel = normalizeMonthOptionValue(leg.month);
            if (monthLabel) {
                codeLabel = `${codeLabel} ${monthLabel}`.trim();
            }
        }
        if (codeLabel && leg && leg.month) {
            const monthToken = (parseMonthSelection(leg.month)?.month || leg.month).toLowerCase();
            const lowered = codeLabel.toLowerCase();
            if (lowered.startsWith(`${monthToken} `)) {
                codeLabel = codeLabel.slice(leg.month.length + 1);
            }
        }
        if (!codeLabel) return;
        const absRatio = Math.abs(ratio);
        const ratioLabel = absRatio === 1 ? '' : absRatio.toString();
        let prefix = '';
        if (ratio < 0) {
            prefix = '-';
        } else if (parts.length) {
            prefix = '+';
        }
        const token = `${prefix}${ratioLabel ? ratioLabel : ''} ${codeLabel}`.trim();
        parts.push(token);
    });
    return parts.join(' ');
}

function getLegFormulaLabel() {
    return buildLegFormula(getLegsForDisplay());
}

function getLegFormulaLabelWithMonths() {
    return buildLegFormula(getLegsForDisplay(), { includeMonth: true });
}

function formatMonthLabelCompact(label) {
    if (!label) return '';
    const normalized = normalizeMonthOptionValue(label);
    return String(normalized).replace(/\s*\+\s*/g, '+');
}

function buildCleanTitle(legs, baseYear) {
    const tokens = [];
    legs.forEach((leg) => {
        const ratio = Number(leg.ratio);
        if (!Number.isFinite(ratio) || ratio === 0) return;
        const resolved = resolveCommodityForLeg(leg);
        const root = (resolved && (resolved.root_code || resolved.code)) || leg.code || '';
        if (!root) return;
        const fullContract = buildContractCode(root, leg.month, baseYear);
        if (!fullContract) return;
        const rootConfig = getRootConfig(root) || {};
        const yellowKey = String(rootConfig.yellow_key || '').trim();
        const trimmedContract = fullContract.trim();
        const suffix = yellowKey ? ` ${yellowKey}` : '';
        const monthToken = suffix && trimmedContract.toLowerCase().endsWith(suffix.toLowerCase())
            ? trimmedContract.slice(0, -suffix.length).trim()
            : trimmedContract;
        const absRatio = Math.abs(ratio);
        const ratioToken = absRatio === 1 ? monthToken : `${absRatio}*${monthToken}`;
        tokens.push({ sign: ratio < 0 ? '-' : '+', text: ratioToken });
    });
    if (!tokens.length) return '';
    const [first, ...rest] = tokens;
    const parts = [first.sign === '-' ? `-${first.text}` : first.text];
    rest.forEach((item) => {
        parts.push(`${item.sign} ${item.text}`);
    });
    return parts.join(' ').replace(/\+\s-/g, '- ');
}

function getCleanChartTitle(baseYear) {
    const legs = getLegsForDisplay();
    if (!legs.length) return '';
    return buildCleanTitle(legs, baseYear);
}

function updateLegSummary() {
    const summary = document.getElementById('leg-summary');
    if (!summary) return;
    const formula = getLegFormulaLabel();
    const modeLabel = LEG_MODE_CONFIG[legState.mode].label;
    summary.textContent = formula
        ? (state.spreadMode === 'prebuilt' ? formula : `${formula} ${modeLabel}`)
        : '';
}

function updatePrebuiltSummary(prebuilt, warning) {
    const summary = document.getElementById('prebuilt-summary');
    if (!summary) return;
    if (!prebuilt) {
        summary.textContent = '';
        summary.classList.remove('has-warning');
        return;
    }
    const base = prebuilt.formula || prebuilt.label || '';
    summary.textContent = warning ? `${base} · ${warning}` : base;
    summary.classList.toggle('has-warning', Boolean(warning));
}

function getSpreadLabel() {
    if (state.spreadMode === 'prebuilt') {
        const selected = PREBUILT_SPREADS.find((spread) => spread.id === state.prebuiltId);
        const fallback = selected ? selected.label : '';
        return state.customSpreadName || fallback;
    }
    return state.customSpreadName || '';
}

function getTradingViewBaseSymbol(root, name) {
    if (!root && !name) return '';
    const rootConfig = getRootConfig(root);
    if (rootConfig && rootConfig.tradingview_symbol) {
        return rootConfig.tradingview_symbol;
    }
    const embedded = window.EMBEDDED_DATA;
    const meta = embedded && embedded.meta ? embedded.meta : {};
    const tvSymbols = meta.tradingview_symbols || meta.tradingviewSymbols || {};
    const nameKey = name ? String(name).trim() : '';
    if (nameKey && tvSymbols[nameKey]) return tvSymbols[nameKey];
    return tvSymbols[root] || DEFAULT_TRADINGVIEW_SYMBOLS[root] || root;
}

function getMonthCode(monthLabel) {
    const selection = parseMonthSelection(monthLabel);
    const month = selection && selection.rollMonth ? selection.rollMonth : (selection && selection.month ? selection.month : monthLabel);
    const entry = MONTH_CODES.find((item) => item.m.toLowerCase() === String(month).toLowerCase());
    return entry ? entry.c : '';
}

function getContractMonthCode(monthLabel) {
    const selection = parseMonthSelection(monthLabel);
    const month = selection && selection.month ? selection.month : (selection && selection.rollMonth ? selection.rollMonth : monthLabel);
    const entry = MONTH_CODES.find((item) => item.m.toLowerCase() === String(month).toLowerCase());
    return entry ? entry.c : '';
}

function getContractYearForMonthLabel(baseYear, monthLabel) {
    const selection = parseMonthSelection(monthLabel);
    const offset = selection ? Number(selection.offset || 0) : 0;
    const contractYear = TRADE_MATH.contractYearForOffset(baseYear, offset);
    return contractYear == null ? baseYear : contractYear;
}

function getLegContractYearOffset(leg) {
    if (!leg || !leg.month || isFlatCurveLeg(leg)) return 0;
    const selection = parseMonthSelection(leg.month);
    const offset = selection ? Number(selection.offset || 0) : 0;
    return Number.isFinite(offset) ? Math.trunc(offset) : 0;
}

function rebaseLegSeriesToFormulaYears(item) {
    if (!item || !item.series) return item;
    const offset = getLegContractYearOffset(item.leg);
    return {
        ...item,
        series: TRADE_MATH.rebaseSeriesYears(item.series, offset),
        vol30Series: item.vol30Series
            ? TRADE_MATH.rebaseSeriesYears(item.vol30Series, offset)
            : item.vol30Series
    };
}

function buildContractCode(rootCode, monthLabel, baseYear) {
    if (!rootCode || !baseYear) return '';
    const rootConfig = getRootConfig(rootCode) || {};
    const configuredRoot = rootConfig.root || canonicalizeRootCode(rootCode);
    if (isFlatCurveRoot(configuredRoot)) {
        const fullYear = Number(baseYear);
        const yy = Number.isFinite(fullYear) ? String(fullYear).slice(-2).padStart(2, '0') : '';
        const y = yy ? yy.slice(-1) : '';
        return TRADE_MATH.applyTickerTemplate(
            rootConfig.ticker_template || '{root} {yellow_key}',
            {
                root: configuredRoot,
                yellow_key: rootConfig.yellow_key || '',
                year: Number.isFinite(fullYear) ? String(fullYear) : '',
                yyyy: Number.isFinite(fullYear) ? String(fullYear) : '',
                yy,
                year_2d: yy,
                y,
                year_1d: y,
                month_code: ''
            }
        );
    }
    if (!monthLabel) return '';
    const monthCode = getContractMonthCode(monthLabel);
    if (!monthCode) return '';
    const contractYear = getContractYearForMonthLabel(baseYear, monthLabel);
    return TRADE_MATH.buildTicker(configuredRoot, monthCode, contractYear, rootConfig);
}

function buildContractFormulaForYear(year) {
    const legs = getLegsForDisplay();
    if (!legs.length || !Number.isFinite(year)) return '';
    const parts = [];
    legs.forEach((leg) => {
        const ratio = Number(leg.ratio);
        if (!Number.isFinite(ratio) || ratio === 0) return;
        const resolved = resolveCommodityForLeg(leg);
        const root = (resolved && (resolved.root_code || resolved.code)) || leg.code || state.commodity;
        const contract = buildContractCode(root, leg.month, year);
        if (!contract) return;
        const absRatio = Math.abs(ratio);
        const token = absRatio === 1 ? contract : `${absRatio}*${contract}`;
        parts.push(ratio < 0 ? `-${token}` : token);
    });
    return parts.join(' + ').replace(/\s\+\s-/g, ' - ');
}

function getHoverContractLabel(year) {
    const legs = getLegsForDisplay();
    if (!legs.length) return '';
    const leg = legs[0];
    if (!leg || (!leg.month && !isFlatCurveLeg(leg))) return '';
    const resolved = resolveCommodityForLeg(leg);
    const root = (resolved && (resolved.root_code || resolved.code)) || leg.code || state.commodity;
    return buildContractCode(root, leg.month, year);
}

function buildHoverCustomdata(labels, contractLabel, formulaLabel) {
    if (!Array.isArray(labels)) return [];
    if (!contractLabel) {
        return labels.map((label) => [label ?? '', formulaLabel || '']);
    }
    return labels.map((label) => [label ?? '', contractLabel, formulaLabel || '']);
}

function buildTradingViewSymbol(baseSymbol, monthCode, year) {
    if (!baseSymbol || !monthCode || !year) return '';
    const parts = String(baseSymbol).split(':');
    const exchange = parts.length > 1 ? parts[0] : '';
    const root = parts.length > 1 ? parts.slice(1).join(':') : parts[0];
    const contract = `${root}${monthCode}${year}`;
    return exchange ? `${exchange}:${contract}` : contract;
}

function getTradingViewContractSymbol(leg, baseYear) {
    const resolved = resolveCommodityForLeg(leg);
    const root = (resolved && (resolved.root_code || resolved.security || resolved.code)) || leg.code;
    if (!root) return '';
    const name = resolved && (resolved.clean_name || resolved.name);
    const selection = parseMonthSelection(leg.month || '');
    const contractInfo = resolved ? getContractInfo(resolved) : { month: null };
    const month = (selection && selection.month) || contractInfo.month || leg.month;
    const monthCode = getMonthCode(month);
    if (!monthCode) return '';
    const offset = selection ? selection.offset : 0;
    const year = Number(baseYear) + (Number.isFinite(offset) ? offset : 0);
    const baseSymbol = getTradingViewBaseSymbol(root, name);
    return buildTradingViewSymbol(baseSymbol, monthCode, year);
}

function buildTradingViewUrl(expression) {
    if (!expression) return '';
    return `https://www.tradingview.com/chart/?symbol=${encodeURIComponent(expression)}`;
}

function formatRatioValue(value) {
    return TRADE_MATH.format(value);
}

function getTradingViewExpression() {
    const legs = getLegsForCalculation();
    if (!legs.length) return '';
    const baseYear = getTradingViewBaseYear();
    const parts = [];
    legs.forEach((leg, index) => {
        const ratio = getEffectiveLegRatio(leg, index);
        if (!ratio) return;
        const symbol = getTradingViewContractSymbol(leg, baseYear);
        if (!symbol) return;
        const absRatio = Math.abs(ratio);
        const coeff = absRatio === 1 ? '' : `${formatRatioValue(absRatio)}*`;
        const prefix = ratio < 0 ? '-' : (parts.length ? '+' : '');
        parts.push(`${prefix}${coeff}${symbol}`);
    });
    return parts.join('');
}

function getTradingViewBaseYear() {
    if (lastRenderedData) {
        const primary = getPrimarySeries(lastRenderedData);
        if (primary && primary.year) {
            return primary.year;
        }
    }
    return getLatestYear();
}

function openTradingViewExternal() {
    const expression = getTradingViewExpression();
    if (!expression) {
        alert('TradingView needs a valid spread with month selection.');
        return;
    }
    const url = buildTradingViewUrl(expression);
    state.showTradingView = false;
    document.querySelectorAll('[data-toggle=\"tradingview\"]').forEach((toggle) => toggle.classList.remove('active'));
    scheduleUserSettingsSave();
    window.location.href = url;
}

function getRootCodeOptions() {
    const commodities = getCommodityList();
    const unique = new Map();
    getRootConfigEntries().forEach((config) => {
        const unit = TRADE_MATH.normalizeUnit(config.native_unit);
        const unitDetail = unit ? ` · ${unit}` : '';
        const curveDetail = TRADE_MATH.normalizeCurveMode(config.curve_mode) === 'flat'
            ? ' · flat daily'
            : '';
        unique.set(config.root, {
            value: config.root,
            label: `${config.name || config.root} — ${config.root}${unitDetail}${curveDetail}`,
            sortOrder: Number.isFinite(Number(config.sort_order)) ? Number(config.sort_order) : Number.MAX_SAFE_INTEGER
        });
    });
    commodities.forEach((com) => {
        const sourceRoot = com.security || com.root_code || com.code;
        const root = canonicalizeRootCode(sourceRoot);
        const label = com.clean_name || com.name || root;
        if (root && !unique.has(root)) {
            const config = getRootConfig(root);
            const unit = config && TRADE_MATH.normalizeUnit(config.native_unit);
            const unitDetail = unit ? ` · ${unit}` : '';
            const curveDetail = config && TRADE_MATH.normalizeCurveMode(config.curve_mode) === 'flat'
                ? ' · flat daily'
                : '';
            unique.set(root, {
                value: root,
                label: `${label} — ${root}${unitDetail}${curveDetail}`,
                sortOrder: config && Number.isFinite(Number(config.sort_order))
                    ? Number(config.sort_order)
                    : Number.MAX_SAFE_INTEGER
            });
        }
    });
    return Array.from(unique.values()).sort((a, b) => (
        (a.sortOrder - b.sortOrder) || a.value.localeCompare(b.value)
    ));
}

function getReferenceIndex(com, info) {
    if (com && com.reference != null) {
        const ref = Number(com.reference);
        if (Number.isFinite(ref)) return ref;
    }
    if (info && Number.isFinite(info.yearIndex)) {
        return info.yearIndex;
    }
    return null;
}

function getSelectionMatchLabel(selection, fallback) {
    if (selection && selection.month) {
        return normalizePeriodLabel(selection.month);
    }
    return normalizePeriodLabel(fallback || '');
}

function formatPeriodOffset(label, offset) {
    if (!offset) return label;
    return `${label} + ${offset}`;
}

function getMonthOptionsForRoot(rootCode) {
    const commodities = getCommodityList();
    if (rootCode && isFlatCurveRoot(rootCode)) return [];
    if (!rootCode || !commodities.length) return EXTENDED_MONTH_OPTIONS.slice();
    const canonicalRoot = canonicalizeRootCode(rootCode);
    const matches = commodities.filter((com) => (
        canonicalizeRootCode(com.security || com.root_code || com.code) === canonicalRoot
    ));
    if (!matches.length) return EXTENDED_MONTH_OPTIONS.slice();

    const monthSet = new Set();
    const quarterSet = new Set();
    const halfSet = new Set();
    let hasMonthly = false;
    let hasReference2 = false;

    matches.forEach((com) => {
        const period = normalizePeriodLabel(com.contract_month || com.contractMonth || com.month || '');
        const frequency = normalizeFrequencyLabel(com.frequency);
        if (period) {
            if (period.startsWith('Q')) {
                quarterSet.add(period);
            } else if (period.startsWith('Half')) {
                halfSet.add(period);
            } else {
                monthSet.add(period);
                hasMonthly = true;
            }
        }
        if (frequency === 'Monthly') hasMonthly = true;
        if (frequency === 'Quarterly' && !period) {
            ['Q1', 'Q2', 'Q3', 'Q4'].forEach((q) => quarterSet.add(q));
        }
        if (frequency === 'Half' && !period) {
            ['Half 1', 'Half 2'].forEach((h) => halfSet.add(h));
        }
        if (Number(com.reference) === 2) hasReference2 = true;
    });

    const monthOrder = MONTH_CODES.map((item) => item.m);
    const months = monthOrder.filter((month) => monthSet.has(month));
    const quarterlyOnly = !months.length && (quarterSet.size || halfSet.size);
    const includeDerived = hasMonthly && !quarterlyOnly;
    const quarters = ['Q1', 'Q2', 'Q3', 'Q4'].filter(
        (q) => quarterSet.has(q) || includeDerived || quarterlyOnly
    );
    const halves = ['Half 1', 'Half 2'].filter(
        (h) => halfSet.has(h) || includeDerived || quarterlyOnly
    );

    const baseOptions = [];
    if (!quarterlyOnly) baseOptions.push(...months);
    baseOptions.push(...quarters, ...halves);

    if (!baseOptions.length) {
        return EXTENDED_MONTH_OPTIONS.slice();
    }

    if (hasReference2) {
        const plusOptions = baseOptions.map((label) => formatPeriodOffset(label, 1));
        return baseOptions.concat(plusOptions);
    }
    return baseOptions;
}

function populateMonthSelect(select, options, currentValue) {
    if (!select) return;
    select.innerHTML = '';
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Select';
    select.appendChild(placeholder);
    options.forEach((month) => {
        const option = document.createElement('option');
        option.value = month;
        option.textContent = month;
        select.appendChild(option);
    });
    if (currentValue && options.includes(currentValue)) {
        select.value = currentValue;
    } else {
        select.value = '';
    }
}

function updateMonthOptionsForLeg(legIndex) {
    const select = document.querySelector(`.leg-month[data-leg="${legIndex + 1}"]`);
    if (!select) return;
    const leg = legState.legs[legIndex];
    const flatCurve = isFlatCurveRoot(leg.code);
    const options = getMonthOptionsForRoot(leg.code);
    if (flatCurve) leg.month = '';
    const normalized = normalizeMonthOptionValue(leg.month);
    populateMonthSelect(select, options, normalized);
    select.disabled = flatCurve;
    if (normalized && !options.includes(normalized)) {
        leg.month = '';
    }
}

function resolvePrebuiltCode(code) {
    if (!code) return code;
    const embedded = window.EMBEDDED_DATA;
    const meta = embedded && embedded.meta ? embedded.meta : {};
    const aliases = meta.prebuilt_code_aliases || meta.prebuiltCodeAliases || {};
    const match = aliases[code] || aliases[code.toUpperCase()] || aliases[code.toLowerCase()];
    const candidate = match || code;
    const commodities = getCommodityList();
    const normalized = canonicalizeRootCode(candidate);
    const byCode = commodities.find((com) => {
        const root = com.security || com.root_code || com.code;
        return canonicalizeRootCode(root) === normalized || String(com.code).toUpperCase() === normalized;
    });
    if (byCode) {
        return canonicalizeRootCode(byCode.security || byCode.root_code || byCode.code);
    }
    const nameMap = {
        XB: ['RBOB'],
        HO: ['Heating Oil'],
        CL: ['Crude'],
        WU: ['GC Jet', 'GC Jet Basis'],
        ME: ['GC Jet Basis'],
        LT: ['GC ULSD Basis'],
        NG: ['Nat Gas'],
        BRENT: ['Brent']
    };
    const names = nameMap[normalized];
    if (names) {
        for (const name of names) {
            const target = commodities.find((com) => (com.name || '').toLowerCase() === name.toLowerCase());
            if (target) {
                return target.security || target.root_code || target.code;
            }
        }
    }
    return normalized || candidate;
}

function resolveCommodityForLeg(leg) {
    if (!leg || !leg.code) return null;
    const commodities = getCommodityList();
    if (!commodities.length) return null;
    const root = canonicalizeRootCode(leg.code);
    const flatCurve = isFlatCurveRoot(root);
    const selection = flatCurve ? null : parseMonthSelection(leg.month || '');
    const desiredMonth = flatCurve ? '' : getSelectionMatchLabel(selection, leg.month);
    const desiredOffset = flatCurve ? 0 : (selection ? selection.offset : 0);

    let matches = commodities
        .filter((com) => canonicalizeRootCode(com.security || com.root_code || com.code) === root)
        .map((com) => ({ com, info: getContractInfo(com) }));

    if (desiredMonth) {
        matches = matches.filter((item) => {
            const month = normalizePeriodLabel(item.info.month || item.com.contract_month || item.com.month || '');
            return month && month.toLowerCase() === desiredMonth.toLowerCase();
        });
    }

    if (desiredOffset > 0) {
        const targetRef = desiredOffset + 1;
        const refMatches = matches.filter((item) => getReferenceIndex(item.com, item.info) === targetRef);
        if (refMatches.length) {
            matches = refMatches;
        }
    } else if (matches.some((item) => Number.isFinite(getReferenceIndex(item.com, item.info)))) {
        const refs = matches
            .map((item) => getReferenceIndex(item.com, item.info))
            .filter((value) => Number.isFinite(value));
        const minRef = refs.length ? Math.min(...refs) : null;
        const refMatches = minRef != null
            ? matches.filter((item) => getReferenceIndex(item.com, item.info) === minRef)
            : [];
        if (refMatches.length) {
            matches = refMatches;
        }
    }

    if (!matches.length) {
        matches = commodities
            .filter((com) => canonicalizeRootCode(com.security || com.root_code || com.code) === root)
            .map((com) => ({ com, info: getContractInfo(com) }));
    }
    if (!matches.length) return null;

    matches.sort((a, b) => {
        const yearA = a.info.year || 0;
        const yearB = b.info.year || 0;
        if (yearA !== yearB) return yearA - yearB;
        const idxA = a.info.yearIndex || 0;
        const idxB = b.info.yearIndex || 0;
        if (idxA !== idxB) return idxA - idxB;
        return String(a.com.code || '').localeCompare(String(b.com.code || ''));
    });
    return matches[matches.length - 1].com;
}

function syncCommodityFromLeg() {
    const resolved = resolveCommodityForLeg(legState.legs[0]);
    if (!resolved) {
        state.monthSpecific = false;
        renderComList();
        return;
    }
    state.commodity = resolved.code;
    const selectedMonth = legState.legs[0].month || '';
    const selectedLabel = getSelectionMatchLabel(parseMonthSelection(selectedMonth), selectedMonth);
    const resolvedLabel = normalizePeriodLabel(resolved.contract_month || resolved.month || '');
    state.monthSpecific = Boolean(
        resolvedLabel &&
        selectedLabel &&
        resolvedLabel.toLowerCase() === selectedLabel.toLowerCase()
    );
    renderComList();
}

// LOGIC
function toggleYear(year) {
    if (state.years.includes(year)) {
        state.years = state.years.filter(y => y !== year);
    } else {
        state.years.push(year);
    }
    renderYearGrid();
    updateChart();
    scheduleUserSettingsSave();
}

function selectCom(code) {
    const commodities = getCommodityList();
    const selected = commodities.find((com) => com.code === code || com.root_code === code || com.security === code);
    if (selected) {
        legState.legs[0].code = selected.security || selected.root_code || selected.code;
        if (isFlatCurveRoot(legState.legs[0].code)) {
            legState.legs[0].month = '';
        } else if (selected.contract_month) {
            legState.legs[0].month = selected.contract_month;
        }
    } else {
        legState.legs[0].code = code;
    }
    const codeSelect = document.querySelector('.leg-code[data-leg="1"]');
    if (codeSelect) {
        codeSelect.value = legState.legs[0].code;
    }
    const monthSelect = document.querySelector('.leg-month[data-leg="1"]');
    if (monthSelect) {
        monthSelect.value = legState.legs[0].month;
    }
    updateMonthOptionsForLeg(0);
    updateLegFieldStates();
    syncCommodityFromLeg();
    updateLegSummary();
    updateChart();
    scheduleUserSettingsSave();
}

function setUnit(unit, el) {
    state.unit = unit;
    syncUnitUI();
    updateChart();
    scheduleUserSettingsSave();
}

function setField(fieldKey) {
    const availableOptions = getAvailableFieldOptions();
    const allowed = new Set(availableOptions.map((option) => option.key));
    const nextField = allowed.has(fieldKey)
        ? fieldKey
        : getDefaultFieldOptionKey(availableOptions);
    if (nextField === state.field) return;
    state.field = nextField;
    syncFieldUI();
    updateChart();
    scheduleUserSettingsSave();
}

function syncUnitUI() {
    document.querySelectorAll('.unit-opt').forEach((opt) => {
        const unit = opt.dataset.unit;
        opt.classList.toggle('active', unit === state.unit);
    });
}

function syncFieldUI() {
    const available = new Set(getAvailableFieldOptions().map((option) => option.key));
    document.querySelectorAll('[data-field]').forEach((button) => {
        const key = button.dataset.field;
        button.hidden = !available.has(key);
        button.classList.toggle('active', key === state.field);
    });
    const select = document.getElementById('field-select');
    if (select && select.value !== state.field) {
        select.value = state.field;
    }
}

function normalizeRootConfigEntry(value, fallbackRoot) {
    const entry = value && typeof value === 'object' ? value : {};
    const root = String(
        entry.root || entry.security_root || entry.securityRoot || entry.security || entry.code || fallbackRoot || ''
    ).trim().toUpperCase();
    if (!root) return null;
    const nativeUnitRaw = entry.native_unit || entry.nativeUnit || entry.unit || '';
    const nativeUnit = TRADE_MATH.normalizeUnit(nativeUnitRaw);
    const aliasesRaw = entry.aliases || entry.alias || [];
    const aliases = (Array.isArray(aliasesRaw) ? aliasesRaw : String(aliasesRaw).split(/[|,;]/))
        .map((alias) => String(alias || '').trim().toUpperCase())
        .filter(Boolean);
    return {
        ...entry,
        root,
        name: entry.common_name || entry.name || entry.display_name || entry.displayName || entry.clean_name || root,
        native_unit: nativeUnit || String(nativeUnitRaw || '').trim(),
        yellow_key: entry.yellow_key || entry.yellowKey || '',
        ticker_template: entry.ticker_template || entry.tickerTemplate || '{root}{month_code}{y} {yellow_key}',
        curve_mode: TRADE_MATH.normalizeCurveMode(entry.curve_mode || entry.curveMode),
        tradingview_symbol: entry.tradingview_symbol || entry.tradingviewSymbol || '',
        bbl_per_mt: entry.bbl_per_mt != null ? entry.bbl_per_mt : entry.bblPerMT,
        gal_per_bbl: entry.gal_per_bbl != null ? entry.gal_per_bbl : entry.galPerBbl,
        aliases,
        enabled: entry.enabled !== false
    };
}

function collectRootConfigCandidates(rawConfig) {
    if (!rawConfig) return [];
    if (Array.isArray(rawConfig)) {
        return rawConfig.map((entry) => [null, entry]);
    }
    if (typeof rawConfig !== 'object') return [];
    const roots = rawConfig.roots || rawConfig.enabled_roots || rawConfig.enabledRoots;
    if (Array.isArray(roots)) {
        return roots.map((entry) => [typeof entry === 'string' ? entry : null, entry]);
    }
    if (roots && typeof roots === 'object') {
        return Object.entries(roots);
    }
    return Object.entries(rawConfig).filter((entry) => {
        const value = entry[1];
        return value && typeof value === 'object' && !Array.isArray(value);
    });
}

function getRootConfigEntries() {
    const embedded = window.EMBEDDED_DATA;
    const meta = embedded && embedded.meta ? embedded.meta : {};
    const rawConfig = meta.root_config || meta.rootConfig || null;
    const candidates = collectRootConfigCandidates(rawConfig);
    const merged = new Map();

    if (!candidates.length) {
        Object.entries(FALLBACK_ROOT_CONFIG).forEach(([root, config]) => {
            merged.set(root, normalizeRootConfigEntry(config, root));
        });
    }

    candidates.forEach(([key, value]) => {
        const valueObject = value && typeof value === 'object' ? value : {};
        const entry = normalizeRootConfigEntry(valueObject, key);
        if (!entry) return;
        if (!entry.enabled) {
            merged.delete(entry.root);
            return;
        }
        merged.set(entry.root, entry);
    });

    return Array.from(merged.values()).filter(Boolean);
}

function getRootConfig(rootCode) {
    const normalized = String(rootCode || '').trim().toUpperCase();
    if (!normalized) return null;
    return getRootConfigEntries().find((entry) => (
        entry.root === normalized || (Array.isArray(entry.aliases) && entry.aliases.includes(normalized))
    )) || null;
}

function isFlatCurveRoot(rootCode) {
    const config = getRootConfig(rootCode);
    return Boolean(config && TRADE_MATH.normalizeCurveMode(config.curve_mode) === 'flat');
}

function isFlatCurveLeg(leg, resolved) {
    return isFlatCurveRoot(getRootCodeFromLeg(leg, resolved));
}

function canonicalizeRootCode(rootCode) {
    const normalized = String(rootCode || '').trim().toUpperCase();
    if (!normalized) return '';
    const config = getRootConfig(normalized);
    return config ? config.root : normalized;
}

function getRootCodeFromLeg(leg, resolved) {
    return canonicalizeRootCode(String(
        (resolved && (resolved.root_code || resolved.security || resolved.code)) ||
        (leg && leg.code) ||
        ''
    ).trim().toUpperCase());
}

function getRootConfigForLeg(leg, resolved) {
    return getRootConfig(getRootCodeFromLeg(leg, resolved));
}

function getCommodityList() {
    const embedded = window.EMBEDDED_DATA;
    if (embedded && embedded.meta && Array.isArray(embedded.meta.commodities) && embedded.meta.commodities.length) {
        return embedded.meta.commodities;
    }
    return COMMODITIES;
}

function parseYearValue(value) {
    if (value == null) return null;
    const match = String(value).match(/(\d{2,4})/);
    if (!match) return null;
    let year = Number(match[1]);
    if (!Number.isFinite(year)) return null;
    if (year < 100) {
        year += year >= 70 ? 1900 : 2000;
    }
    return year;
}

function parseRangeValue(value) {
    if (!value) return null;
    const match = String(value).match(/(\d{2,4})\s*-\s*(\d{2,4})/);
    if (!match) return null;
    const start = parseYearValue(match[1]);
    const end = parseYearValue(match[2]);
    if (!start || !end) return null;
    return { start, end };
}

function getSidebarCommodityList() {
    const embedded = window.EMBEDDED_DATA;
    const configuredSidebar = embedded && embedded.meta && Array.isArray(embedded.meta.sidebar)
        ? embedded.meta.sidebar
        : [];
    const commodities = configuredSidebar.length ? configuredSidebar : getCommodityList();
    const unique = new Map();

    commodities.forEach((com) => {
        const root = canonicalizeRootCode(com.security || com.root_code || com.code);
        if (!root) return;
        const rootConfig = getRootConfig(root);
        const entry = unique.get(root) || {
            code: root,
            root_code: root,
            name: (rootConfig && rootConfig.name) || com.name || com.security || root,
            minYear: null,
            maxYear: null,
            sort_order: rootConfig && Number.isFinite(Number(rootConfig.sort_order))
                ? Number(rootConfig.sort_order)
                : Number.MAX_SAFE_INTEGER,
            product_group: (rootConfig && rootConfig.product_group) || ''
        };

        const parsedRange = parseRangeValue(com.rng);
        if (parsedRange) {
            entry.minYear = entry.minYear == null ? parsedRange.start : Math.min(entry.minYear, parsedRange.start);
            entry.maxYear = entry.maxYear == null ? parsedRange.end : Math.max(entry.maxYear, parsedRange.end);
        }

        const contractYear = parseYearValue(com.contract_year || com.contract_month_yr);
        if (contractYear) {
            entry.minYear = entry.minYear == null ? contractYear : Math.min(entry.minYear, contractYear);
            entry.maxYear = entry.maxYear == null ? contractYear : Math.max(entry.maxYear, contractYear);
        }

        if (com.name && (!entry.name || entry.name === entry.code)) {
            entry.name = com.name;
        }

        unique.set(root, entry);
    });

    getRootConfigEntries().forEach((config) => {
        const existing = unique.get(config.root) || {
            code: config.root,
            root_code: config.root,
            name: config.name || config.root,
            minYear: null,
            maxYear: null
        };
        existing.name = config.name || existing.name;
        existing.native_unit = TRADE_MATH.normalizeUnit(config.native_unit) || config.native_unit || '';
        existing.sort_order = Number.isFinite(Number(config.sort_order))
            ? Number(config.sort_order)
            : existing.sort_order;
        existing.product_group = config.product_group || existing.product_group || '';
        unique.set(config.root, existing);
    });

    return Array.from(unique.values())
        .map((entry) => ({
            code: entry.code,
            root_code: entry.root_code,
            name: entry.name || entry.code,
            native_unit: entry.native_unit || '',
            sort_order: Number.isFinite(Number(entry.sort_order)) ? Number(entry.sort_order) : Number.MAX_SAFE_INTEGER,
            product_group: entry.product_group || '',
            rng: entry.minYear && entry.maxYear ? `${entry.minYear}-${entry.maxYear}` : '--'
        }))
        .sort((a, b) => (a.sort_order - b.sort_order) || a.code.localeCompare(b.code));
}

function getAvailableYears() {
    const embedded = window.EMBEDDED_DATA;
    if (embedded && embedded.meta && Array.isArray(embedded.meta.years) && embedded.meta.years.length) {
        return embedded.meta.years.map(Number).sort((a, b) => a - b);
    }
    return [];
}

function getDefaultSelectedYears(availableYears) {
    const currentYear = new Date().getFullYear();
    const defaults = [currentYear - 1, currentYear, currentYear + 1];
    if (!Array.isArray(availableYears) || !availableYears.length) return defaults;
    const available = new Set(availableYears.map(Number).filter(Number.isFinite));
    const selected = defaults.filter((year) => available.has(year));
    return selected.length ? selected : Array.from(available).sort((a, b) => a - b).slice(-3);
}

function getLatestYear() {
    const years = getAvailableYears();
    if (years.length) return years[years.length - 1];
    return 2026;
}

function getBaseUnit() {
    const embedded = window.EMBEDDED_DATA;
    if (embedded && embedded.meta && embedded.meta.unit) {
        return TRADE_MATH.normalizeUnit(embedded.meta.unit) || embedded.meta.unit;
    }
    return '$/bbl';
}

function getSelectedRootCode() {
    const resolved = resolveCommodityForLeg(legState.legs[0]);
    if (!resolved && !legState.legs[0].code) return '';
    if (resolved && resolved.root_code) return resolved.root_code;
    if (resolved && resolved.code) return resolved.code;
    return state.commodity;
}

function getSelectedRootCodes() {
    const codes = new Set();
    getLegsForDisplay().forEach((leg) => {
        const resolved = resolveCommodityForLeg(leg);
        if (resolved && resolved.root_code) {
            codes.add(canonicalizeRootCode(resolved.root_code));
        }
        if (resolved && resolved.code) {
            codes.add(resolved.code);
        }
        if (!resolved && leg.code) {
            codes.add(canonicalizeRootCode(leg.code));
        }
    });
    if (!codes.size && state.commodity) {
        codes.add(state.commodity);
    }
    return codes;
}

function getConversionConfigForLeg(leg, resolved) {
    const embedded = window.EMBEDDED_DATA;
    const meta = embedded && embedded.meta ? embedded.meta : {};
    const factors = meta.unit_factors || meta.unitFactors || {};
    const root = (resolved && (resolved.root_code || resolved.code)) || leg.code;
    const code = (resolved && resolved.code) || leg.code;
    const rootConfig = getRootConfigForLeg(leg, resolved);
    const selection =
        (rootConfig && {
            bbl_per_mt: rootConfig.bbl_per_mt,
            gal_per_bbl: rootConfig.gal_per_bbl
        }) ||
        factors[code] ||
        factors[root] ||
        factors.default ||
        factors.base ||
        UNIT_FACTORS_BY_CODE[code] ||
        UNIT_FACTORS_BY_CODE[root] ||
        UNIT_FACTORS_BY_CODE.default;
    return TRADE_MATH.normalizeConversionConfig(selection);
}

function getNativeUnitForLeg(leg, resolved) {
    const rootConfig = getRootConfigForLeg(leg, resolved);
    if (rootConfig) {
        return TRADE_MATH.normalizeUnit(rootConfig.native_unit);
    }
    const embedded = window.EMBEDDED_DATA;
    const commodityData = embedded && embedded.commodities && resolved
        ? embedded.commodities[resolved.code]
        : null;
    const candidates = [
        resolved && (resolved.native_unit || resolved.nativeUnit || resolved.unit),
        commodityData && (commodityData.native_unit || commodityData.nativeUnit || commodityData.unit),
        getBaseUnit()
    ];
    for (const candidate of candidates) {
        const normalized = TRADE_MATH.normalizeUnit(candidate);
        if (normalized) return normalized;
    }
    return '';
}

function convertLegValue(value, targetUnit, leg, resolved) {
    const nativeUnit = getNativeUnitForLeg(leg, resolved);
    if (!nativeUnit) return null;
    return TRADE_MATH.convertValue(
        value,
        nativeUnit,
        targetUnit,
        getConversionConfigForLeg(leg, resolved)
    );
}

function getMonthIndex(monthLabel) {
    if (!monthLabel) return 1;
    const selection = parseMonthSelection(monthLabel);
    const baseMonth = selection && selection.rollMonth ? selection.rollMonth : (selection && selection.month ? selection.month : monthLabel);
    const match = MONTH_CODES.find((month) => month.m.toLowerCase() === String(baseMonth).toLowerCase());
    return match ? match.n : 1;
}

function getOffsetMonth(monthLabel, offset) {
    if (!monthLabel) return '';
    const selection = parseMonthSelection(monthLabel);
    if (!selection || !selection.month) return monthLabel;
    const baseMonth = selection.rollMonth || selection.month;
    const baseIndex = MONTH_CODES.findIndex((month) => month.m.toLowerCase() === String(baseMonth).toLowerCase());
    if (baseIndex < 0) return monthLabel;
    const monthShift = offset || 0;
    const zeroBased = baseIndex + monthShift;
    const monthIndex = ((zeroBased % MONTH_CODES.length) + MONTH_CODES.length) % MONTH_CODES.length;
    const yearOffset = (selection.offset || 0) + Math.floor(zeroBased / MONTH_CODES.length);
    const month = MONTH_CODES[monthIndex].m;
    return formatMonthOffset(month, yearOffset);
}

function parseMonthSelection(label) {
    if (!label) return null;
    const text = String(label).trim();
    if (!text) return null;
    const plusParts = text.split('+').map((part) => part.trim()).filter(Boolean);
    if (plusParts.length === 2) {
        const base = normalizePeriodLabel(plusParts[0]);
        const offset = Number(plusParts[1]);
        if (base && Number.isFinite(offset)) {
            const rollMonth = getRollMonthForPeriod(base);
            return { label: `${base} + ${offset}`, month: base, offset, rollMonth };
        }
    }
    const normalized = normalizePeriodLabel(text);
    if (normalized && (normalized.startsWith('Q') || normalized.startsWith('Half'))) {
        const rollMonth = getRollMonthForPeriod(normalized);
        return { label: normalized, month: normalized, offset: getRollYearOffset(rollMonth), rollMonth };
    }
    const monthMatch = MONTH_CODES.find((month) => month.m.toLowerCase() === normalized.toLowerCase());
    if (monthMatch) {
        return { label: monthMatch.m, month: monthMatch.m, offset: 0 };
    }
    return { label: normalized, month: normalized, offset: 0 };
}

function formatMonthOffset(month, offset) {
    if (!offset) return month;
    return `${month} + ${offset}`;
}

function normalizeMonthOptionValue(label) {
    if (!label) return '';
    const selection = parseMonthSelection(label);
    return selection ? selection.label : label;
}

function getDisplayYear(actualYear) {
    return actualYear;
}

function resolveActiveYears(sortedYears) {
    if (!Array.isArray(sortedYears)) return [];
    if (!state.years.length) return sortedYears;
    return sortedYears.filter((year) => state.years.includes(year));
}

function getRollYearOffset(rollMonth) {
    const currentMonth = new Date().getMonth() + 1;
    const rollIndex = getMonthIndex(rollMonth);
    return currentMonth > rollIndex ? 1 : 0;
}

function parseContractMonthYear(value) {
    if (!value) return { month: null, year: null };
    const text = String(value).trim();
    if (!text) return { month: null, year: null };
    const parts = text.split(/\s+/);
    if (parts.length >= 2) {
        const month = normalizeMonthLabel(parts[0]);
        const year = parseYearValue(parts[1]);
        if (month) return { month, year };
    }
    const parsedTicker = TRADE_MATH.parseTicker(text);
    if (parsedTicker) {
        return { month: parsedTicker.month, year: parsedTicker.year };
    }
    const monthCodeMatch = text.toUpperCase().match(/^([FGHJKMNQUVXZ])(\d{1,4})$/);
    if (monthCodeMatch) {
        return {
            month: MONTH_LETTER_MAP[monthCodeMatch[1]] || null,
            year: TRADE_MATH.expandContractYear(monthCodeMatch[2])
        };
    }
    const match = text.match(/^([A-Za-z]{3})(\d{1,4})/);
    if (match) {
        return { month: normalizeMonthLabel(match[1]), year: parseYearValue(match[2]) };
    }
    return { month: normalizeMonthLabel(text), year: null };
}

function normalizeMonthLabel(value) {
    if (!value) return null;
    const key = String(value).slice(0, 3).toUpperCase();
    return MONTH_ABBR_MAP[key] || null;
}

function normalizePeriodLabel(value) {
    if (!value) return '';
    const text = String(value).trim();
    if (!text) return '';
    const upper = text.toUpperCase().replace(/[-_]+/g, ' ').replace(/\s+/g, ' ');
    if (PERIOD_LABELS[upper]) return PERIOD_LABELS[upper];
    const month = normalizeMonthLabel(upper);
    return month || text;
}

function normalizeFrequencyLabel(value) {
    if (!value) return '';
    const upper = String(value).trim().toUpperCase();
    if (!upper) return '';
    if (upper.includes('QUART') || upper.startsWith('Q')) return 'Quarterly';
    if (upper.includes('HALF') || upper.startsWith('H') || upper.startsWith('S')) return 'Half';
    if (upper.includes('MONTH')) return 'Monthly';
    return value;
}

function getRollMonthForPeriod(label) {
    return TRADE_MATH.rollMonthForPeriod(label);
}

function parseMonthYearFromCode(code, root) {
    if (!code) return { month: null, yearDigits: null };
    const cleaned = String(code).toUpperCase().trim();
    const rootToken = root ? String(root).toUpperCase().replace(/\s+/g, '') : '';
    const rootConfig = getRootConfig(rootToken) || {};
    const parsedTicker = TRADE_MATH.parseTicker(cleaned, { ...rootConfig, root: rootToken });
    if (parsedTicker) {
        return { month: parsedTicker.month, yearDigits: parsedTicker.yearDigits };
    }
    let tail = cleaned.replace(/\s+(COMDTY|INDEX|EQUITY|CURNCY)$/i, '').replace(/\s+/g, '');
    if (rootToken && tail.startsWith(rootToken)) {
        tail = tail.slice(rootToken.length);
    }
    const abbrMatch = tail.match(/^(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{1,4})?/);
    if (abbrMatch) {
        return { month: MONTH_ABBR_MAP[abbrMatch[1]], yearDigits: abbrMatch[2] || null };
    }
    const letterMatch = tail.match(/^([FGHJKMNQUVXZ])(\d{1,4})?/);
    if (letterMatch) {
        return { month: MONTH_LETTER_MAP[letterMatch[1]], yearDigits: letterMatch[2] || null };
    }
    return { month: null, yearDigits: null };
}

function getContractInfo(com) {
    const info = { month: null, year: null, yearIndex: null };
    if (!com) return info;

    if (com.contract_month) {
        info.month = com.contract_month;
    }
    if (com.reference != null) {
        const ref = Number(com.reference);
        if (Number.isFinite(ref)) info.yearIndex = ref;
    }
    if (com.contract_year) {
        info.year = parseYearValue(com.contract_year);
    }

    if (!info.month || !info.year) {
        const parsed = parseContractMonthYear(com.contract_month_yr || '');
        if (!info.month && parsed.month) info.month = parsed.month;
        if (!info.year && parsed.year) info.year = parsed.year;
    }

    const parsedCode = parseMonthYearFromCode(com.code, com.security || com.root_code || '');
    if (!info.month && parsedCode.month) {
        info.month = parsedCode.month;
    }
    if (!info.year && parsedCode.yearDigits) {
        if (parsedCode.yearDigits.length === 1) {
            info.yearIndex = Number(parsedCode.yearDigits);
        } else {
            info.year = parseYearValue(parsedCode.yearDigits);
        }
    }

    return info;
}

const MONTH_AXIS_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const MONTH_AXIS_BOUNDARIES = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335, 366];
const ROLLING_AXIS_SPAN = TRADE_MATH.CONTRACT_AXIS_SPAN;
const DERIVED_PERIOD_WINDOWS = {
    Q1: { startMonth: 'Mar', endMonth: 'Dec' },
    Q2: { startMonth: 'Jun', endMonth: 'Mar' },
    Q3: { startMonth: 'Sep', endMonth: 'Jun' },
    Q4: { startMonth: 'Dec', endMonth: 'Sep' },
    'Half 1': { startMonth: 'Jun', endMonth: 'Dec' },
    'Half 2': { startMonth: 'Dec', endMonth: 'Jun' }
};
const DERIVED_PERIOD_COMPONENTS = {
    Q1: ['Jan', 'Feb', 'Mar'],
    Q2: ['Apr', 'May', 'Jun'],
    Q3: ['Jul', 'Aug', 'Sep'],
    Q4: ['Oct', 'Nov', 'Dec'],
    'Half 1': ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun'],
    'Half 2': ['Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
};

function rotateDayOfYear(value, anchorDay, span) {
    if (!Number.isFinite(value) || !Number.isFinite(anchorDay) || !Number.isFinite(span) || span <= 0) {
        return value;
    }
    return TRADE_MATH.rotateCycleDay(value, anchorDay, span);
}

function shiftSeriesX(values, anchorDay, span) {
    if (!Array.isArray(values)) return [];
    if (!Number.isFinite(anchorDay) || !Number.isFinite(span)) return values.slice();
    return values.map((value) => (Number.isFinite(value) ? rotateDayOfYear(value, anchorDay, span) : value));
}

function normalizeSeriesForRollingAxis(xValues, yValues, labels) {
    const normalized = TRADE_MATH.normalizeContractSeries({ x: xValues, y: yValues });
    const normalizedLabels = Array.isArray(labels)
        ? normalized.sourceIndexes.map((index) => labels[index])
        : undefined;
    return { x: normalized.x, y: normalized.y, labels: normalizedLabels };
}

function clipSeriesToSpan(xValues, yValues, labels, span, anchorDay) {
    if (!Array.isArray(xValues) || !Array.isArray(yValues)) {
        return { x: Array.isArray(xValues) ? xValues.slice() : [], y: Array.isArray(yValues) ? yValues.slice() : [], labels: Array.isArray(labels) ? labels.slice() : [] };
    }
    if (!Number.isFinite(span) || span <= 0) {
        return { x: xValues.slice(), y: yValues.slice(), labels: Array.isArray(labels) ? labels.slice() : [] };
    }
    let minX = null;
    let maxX = null;
    if (Number.isFinite(anchorDay)) {
        minX = anchorDay;
        maxX = anchorDay + span - 1;
    } else {
        let maxVal = Number.NEGATIVE_INFINITY;
        xValues.forEach((value) => {
            if (Number.isFinite(value)) {
                maxVal = Math.max(maxVal, value);
            }
        });
        if (!Number.isFinite(maxVal)) {
            return { x: xValues.slice(), y: yValues.slice(), labels: Array.isArray(labels) ? labels.slice() : [] };
        }
        maxX = maxVal;
        minX = maxVal - span + 1;
    }
    const nextX = [];
    const nextY = [];
    const nextL = Array.isArray(labels) ? [] : null;
    for (let i = 0; i < xValues.length; i++) {
        const x = xValues[i];
        if (!Number.isFinite(x)) continue;
        if (x < minX || x > maxX) continue;
        nextX.push(x);
        nextY.push(yValues[i]);
        if (nextL) nextL.push(labels[i]);
    }
    return { x: nextX, y: nextY, labels: nextL || undefined };
}

function clipSeriesToRange(xValues, yValues, labels, range) {
    if (!Array.isArray(range) || range.length !== 2) {
        return {
            x: Array.isArray(xValues) ? xValues.slice() : [],
            y: Array.isArray(yValues) ? yValues.slice() : [],
            labels: Array.isArray(labels) ? labels.slice() : undefined
        };
    }
    const [minimum, maximum] = range;
    const nextX = [];
    const nextY = [];
    const nextLabels = Array.isArray(labels) ? [] : null;
    const length = Math.min(
        Array.isArray(xValues) ? xValues.length : 0,
        Array.isArray(yValues) ? yValues.length : 0
    );
    for (let index = 0; index < length; index++) {
        const value = xValues[index];
        if (!Number.isFinite(value) || value < minimum || value > maximum) continue;
        nextX.push(value);
        nextY.push(yValues[index]);
        if (nextLabels) nextLabels.push(labels[index]);
    }
    return { x: nextX, y: nextY, labels: nextLabels || undefined };
}

function sortSeriesByX(xValues, yValues, customdata) {
    const length = Math.min(
        Array.isArray(xValues) ? xValues.length : 0,
        Array.isArray(yValues) ? yValues.length : 0,
        Array.isArray(customdata) ? customdata.length : Number.MAX_SAFE_INTEGER
    );
    const items = [];
    for (let i = 0; i < length; i++) {
        items.push({
            x: xValues[i],
            y: yValues[i],
            c: Array.isArray(customdata) ? customdata[i] : undefined
        });
    }
    items.sort((a, b) => {
        const ax = Number.isFinite(a.x) ? a.x : Number.POSITIVE_INFINITY;
        const bx = Number.isFinite(b.x) ? b.x : Number.POSITIVE_INFINITY;
        return ax - bx;
    });
    return {
        x: items.map((item) => item.x),
        y: items.map((item) => item.y),
        customdata: Array.isArray(customdata) ? items.map((item) => item.c) : undefined
    };
}

function keepLongestContinuousPlotSeries(xValues, yValues, labels) {
    const continuous = TRADE_MATH.longestContinuousSeries(
        { x: xValues, y: yValues },
        TRADE_MATH.DEFAULT_INTERPOLATION_MAX_SPAN_DAYS
    );
    const nextLabels = Array.isArray(labels)
        ? continuous.sourceIndexes.map((index) => labels[index])
        : undefined;
    return { x: continuous.x, y: continuous.y, labels: nextLabels };
}

function getAxisAnchorDay(seriesMap, years) {
    if (!seriesMap) return null;
    const yearList = Array.isArray(years) && years.length
        ? years
        : Object.keys(seriesMap).map(Number).filter(Number.isFinite);
    const sortedYears = yearList.slice().sort((a, b) => b - a);
    for (const year of sortedYears) {
        const series = seriesMap[year] || seriesMap[String(year)];
        const endDay = TRADE_MATH.latestContractEndDay([series]);
        if (Number.isFinite(endDay)) return endDay;
    }
    return null;
}

function getContractAnchorConfigFromLegs(legs) {
    if (!Array.isArray(legs) || !legs.length) return null;
    let earliestDelivery = null;
    legs.forEach((leg) => {
        if (!leg || !leg.month || isFlatCurveLeg(leg)) return;
        const selection = parseMonthSelection(leg.month);
        const rollMonth = selection && selection.rollMonth
            ? selection.rollMonth
            : (selection && selection.month ? selection.month : leg.month);
        const monthIndex = getMonthIndex(rollMonth);
        if (!Number.isFinite(monthIndex)) return;
        const yearOffset = getLegContractYearOffset(leg);
        const deliveryRank = (yearOffset * 12) + monthIndex;
        if (!earliestDelivery || deliveryRank < earliestDelivery.rank) {
            earliestDelivery = { rank: deliveryRank, monthIndex };
        }
    });
    if (!earliestDelivery) return null;
    const anchorDay = TRADE_MATH.contractCycleAnchorDay(earliestDelivery.monthIndex);
    if (!Number.isFinite(anchorDay)) return null;
    return { anchorDay, rollMonthIndex: earliestDelivery.monthIndex };
}

function getDerivedContractWindow(legs) {
    if (!Array.isArray(legs) || !legs.length) return null;
    const validDays = Array(ROLLING_AXIS_SPAN + 1).fill(true);
    let hasDerivedPeriod = false;

    legs.forEach((leg) => {
        const selection = parseMonthSelection(leg && leg.month ? leg.month : '');
        const period = selection ? selection.month : '';
        const window = DERIVED_PERIOD_WINDOWS[period];
        if (!window) return;
        hasDerivedPeriod = true;
        const startIndex = MONTH_AXIS_LABELS.indexOf(window.startMonth);
        const endIndex = MONTH_AXIS_LABELS.indexOf(window.endMonth);
        const startDay = MONTH_AXIS_BOUNDARIES[startIndex];
        const endDay = MONTH_AXIS_BOUNDARIES[endIndex + 1] - 1;
        for (let day = 1; day <= ROLLING_AXIS_SPAN; day++) {
            const inside = startDay <= endDay
                ? day >= startDay && day <= endDay
                : day >= startDay || day <= endDay;
            validDays[day] = validDays[day] && inside;
        }
    });

    if (!hasDerivedPeriod) return null;
    let bestStart = null;
    let bestLength = 0;
    for (let day = 1; day <= ROLLING_AXIS_SPAN; day++) {
        const previousDay = day === 1 ? ROLLING_AXIS_SPAN : day - 1;
        if (!validDays[day] || validDays[previousDay]) continue;
        let length = 0;
        while (length < ROLLING_AXIS_SPAN) {
            const candidate = ((day - 1 + length) % ROLLING_AXIS_SPAN) + 1;
            if (!validDays[candidate]) break;
            length += 1;
        }
        if (length > bestLength) {
            bestStart = day;
            bestLength = length;
        }
    }
    if (bestStart == null && validDays.slice(1).every(Boolean)) {
        bestStart = 1;
        bestLength = ROLLING_AXIS_SPAN;
    }
    if (bestStart == null || !bestLength) return null;

    const endDay = ((bestStart - 1 + bestLength - 1) % ROLLING_AXIS_SPAN) + 1;
    const displayStart = rotateDayOfYear(bestStart, endDay, ROLLING_AXIS_SPAN);
    return {
        anchorDay: endDay,
        startDay: bestStart,
        endDay,
        displayRange: [displayStart, ROLLING_AXIS_SPAN]
    };
}

function getContractAnchorDayFromLegs(legs) {
    const config = getContractAnchorConfigFromLegs(legs);
    return config ? config.anchorDay : null;
}

function getContractAxisAnchorDay(data, years) {
    const seriesMap = data && data.series ? data.series : data;
    const contractAnchorDay = getContractAnchorDayFromLegs(getLegsForCalculation());
    if (Number.isFinite(contractAnchorDay)) return contractAnchorDay;
    const yearList = Array.isArray(years) && years.length
        ? years.slice().sort((a, b) => b - a)
        : Object.keys(seriesMap || {}).map(Number).filter(Number.isFinite).sort((a, b) => b - a);
    const endByYear = data && data.axisEndByYear ? data.axisEndByYear : {};
    for (const year of yearList) {
        const endDay = Number(endByYear[year] != null ? endByYear[year] : endByYear[String(year)]);
        if (Number.isFinite(endDay)) return endDay;
    }
    const seriesAnchor = getAxisAnchorDay(seriesMap, yearList);
    if (Number.isFinite(seriesAnchor)) return seriesAnchor;
    return null;
}

function getMonthAxisConfig(minX, maxX) {
    const boundaries = MONTH_AXIS_BOUNDARIES;
    const labels = MONTH_AXIS_LABELS;
    const tickvals = [];
    const ticktext = [];
    const gridlines = [];

    for (let i = 0; i < labels.length; i++) {
        const start = boundaries[i];
        const end = boundaries[i + 1];
        if (minX != null && maxX != null && (end < minX || start > maxX)) {
            continue;
        }
        tickvals.push((start + end) / 2);
        ticktext.push(labels[i]);
    }

    boundaries.slice(0, -1).forEach((value) => {
        if (minX != null && maxX != null && (value < minX || value > maxX)) {
            return;
        }
        gridlines.push(value);
    });

    return { tickvals, ticktext, gridlines };
}

function getRollingMonthAxisConfig(anchorDay, span, minX = null, maxX = null) {
    if (!Number.isFinite(anchorDay) || !Number.isFinite(span) || span <= 0) {
        return getMonthAxisConfig(null, null);
    }
    const ticks = [];
    for (let i = 0; i < MONTH_AXIS_LABELS.length; i++) {
        const start = MONTH_AXIS_BOUNDARIES[i];
        const end = MONTH_AXIS_BOUNDARIES[i + 1];
        const mid = (start + end) / 2;
        ticks.push({
            tick: rotateDayOfYear(mid, anchorDay, span),
            label: MONTH_AXIS_LABELS[i],
            gridline: rotateDayOfYear(start, anchorDay, span)
        });
    }
    ticks.sort((a, b) => a.tick - b.tick);
    const filtered = ticks.filter((item) => {
        if (minX == null || maxX == null) return true;
        return item.tick >= minX && item.tick <= maxX;
    });
    const tickvals = filtered.map((item) => item.tick);
    const ticktext = filtered.map((item) => item.label);
    const gridlines = ticks
        .map((item) => item.gridline)
        .filter((value) => (minX == null || maxX == null ? true : value >= minX && value <= maxX))
        .sort((a, b) => a - b);
    return { tickvals, ticktext, gridlines };
}

function buildMonthGridlines() {
    return [];
}

function getPrimarySeries(data) {
    if (!data || !data.series) return null;
    const availableYears = Object.keys(data.series).map(Number).filter(Number.isFinite);
    if (!availableYears.length) return null;

    let candidates = resolveActiveYears(availableYears);
    if (!candidates.length) {
        candidates = availableYears.slice();
    }
    candidates = candidates.slice().sort((a, b) => b - a);

    for (const year of candidates) {
        const series = data.series[year] || data.series[String(year)];
        if (series && Array.isArray(series.y)) {
            return { year, series };
        }
    }

    const fallbackYear = availableYears.sort((a, b) => b - a)[0];
    const fallbackSeries = data.series[fallbackYear] || data.series[String(fallbackYear)];
    return fallbackSeries ? { year: fallbackYear, series: fallbackSeries } : null;
}

function getIndicatorSeries(data) {
    if (!data || !data.series) return null;
    const availableYears = Object.keys(data.series).map(Number).filter(Number.isFinite);
    if (!availableYears.length) return null;
    const latest = Math.max(...availableYears);
    const latestSeries = data.series[latest] || data.series[String(latest)];
    if (latestSeries && Array.isArray(latestSeries.y)) {
        return { year: latest, series: latestSeries };
    }
    const fallback = getPrimarySeries(data);
    return fallback;
}

function applyMonthAdjustmentToSeries(series, monthLabel) {
    if (!series || !Array.isArray(series.y) || !series.y.length) return series;
    const monthIndex = getMonthIndex(monthLabel);
    const avg = series.y.reduce((sum, value) => sum + value, 0) / series.y.length;
    const factor = 1 + ((monthIndex - 6.5) * 0.004);
    const offset = avg * 0.01 * Math.sin((monthIndex / 12) * Math.PI * 2);
    const adjusted = series.y.map((value) => TRADE_MATH.round5(value * factor + offset));
    return { x: series.x.slice(), y: adjusted };
}

function applyMonthAdjustmentToData(data) {
    if (!data || !data.series) return data;
    const monthLabel = legState.legs[0].month;
    if (!monthLabel || state.monthSpecific) return data;

    const adjusted = { ...data, series: {} };
    Object.keys(data.series).forEach((year) => {
        adjusted.series[year] = applyMonthAdjustmentToSeries(data.series[year], monthLabel);
    });

    const primary = getPrimarySeries(adjusted);
    if (primary) {
        adjusted.var = calculateVarStats(primary.series.y);
    }

    return adjusted;
}

function getDefaultFieldKey() {
    const embedded = window.EMBEDDED_DATA;
    if (embedded && embedded.meta && embedded.meta.fields && embedded.meta.fields.default) {
        return String(embedded.meta.fields.default);
    }
    return '';
}

function getSeriesForField(commodityData) {
    if (!commodityData) return null;
    const fields = commodityData.fields || {};
    const defaultField = getDefaultFieldKey();
    const desiredKey = FIELD_KEY_MAP[state.field];
    if (!desiredKey) return commodityData.years;
    if (fields[desiredKey]) return fields[desiredKey];
    if (defaultField === desiredKey) return commodityData.years;
    return null;
}

function getFieldSeriesWithFallback(commodityData, yearX) {
    return getSeriesForField(commodityData);
}

function getEmbeddedSeries() {
    const embedded = window.EMBEDDED_DATA;
    if (!embedded || !embedded.commodities || !embedded.meta) return null;
    const commodityData = embedded.commodities[state.commodity];
    if (!commodityData) return null;
    const selectedLeg = legState.legs[0] || { code: getSelectedRootCode(), month: '' };
    const resolved = resolveCommodityForLeg(selectedLeg);
    const yearX = embedded.meta.yearX || {};
    const fieldSeries = getFieldSeriesWithFallback(commodityData, yearX);
    if (!fieldSeries) return null;
    const series = {};
    const vol30Series = commodityData.volatility_30d || commodityData.vol_30d || null;

    Object.keys(fieldSeries).forEach(year => {
        const seriesData = fieldSeries[year];
        if (!seriesData) return;

        let x = yearX[year] || [];
        let y = [];

        if (Array.isArray(seriesData)) {
            y = seriesData;
        } else if (seriesData && Array.isArray(seriesData.y)) {
            y = seriesData.y;
            if (Array.isArray(seriesData.x)) x = seriesData.x;
        }

        if (!y.length || !x.length) return;

        const scaled = y.map((value) => convertLegValue(value, state.unit, selectedLeg, resolved));
        if (scaled.some((value) => value == null)) return;
        series[year] = normalizeLeapSeries({ x, y: scaled }, year);
    });

    const latestYear = getLatestYear().toString();
    return {
        series,
        var: calculateVarStats(series[latestYear] ? series[latestYear].y : []),
        vol30: vol30Series || null
    };
}

function shouldAdjustMonthForLeg(leg, resolved) {
    if (isFlatCurveLeg(leg, resolved)) return false;
    if (!leg || !leg.month) return false;
    const selection = parseMonthSelection(leg.month);
    const desiredMonth = getSelectionMatchLabel(selection, leg.month);
    if (resolved) {
        const info = getContractInfo(resolved);
        if (info.month) {
            return normalizePeriodLabel(info.month).toLowerCase() !== desiredMonth.toLowerCase();
        }
    }
    return true;
}

function getDirectLegSeriesFromEmbedded(leg) {
    const embedded = window.EMBEDDED_DATA;
    if (!embedded || !embedded.commodities || !embedded.meta) return null;
    const resolved = resolveCommodityForLeg(leg);
    if (!resolved) return null;
    const yearX = embedded.meta.yearX || {};
    const targetReference = getReferenceIndex(resolved, getContractInfo(resolved));
    if (!Number.isFinite(targetReference)) return null;
    const root = canonicalizeRootCode(resolved.root_code || resolved.security || leg.code);
    const selection = parseMonthSelection(leg.month || '');
    const desiredPeriod = normalizePeriodLabel(
        getSelectionMatchLabel(selection, leg.month || '')
    ).toLowerCase();
    const referenceSegments = [];
    let vol30Series = null;

    Object.entries(embedded.commodities).forEach(([code, commodityData]) => {
        const siblingRoot = canonicalizeRootCode(
            commodityData.root_code || commodityData.security || code
        );
        const siblingPeriod = normalizePeriodLabel(
            commodityData.contract_month || commodityData.month || ''
        ).toLowerCase();
        const siblingReference = Number(commodityData.reference);
        if (
            siblingRoot !== root
            || siblingPeriod !== desiredPeriod
            || !Number.isFinite(siblingReference)
        ) {
            return;
        }

        const fieldSeries = getFieldSeriesWithFallback(commodityData, yearX);
        if (!fieldSeries) return;
        const sibling = { ...commodityData, code };
        const segmentSeries = {};
        Object.keys(fieldSeries).forEach((year) => {
            const seriesData = fieldSeries[year];
            if (!seriesData) return;
            let x = yearX[year] || [];
            let y = [];
            if (Array.isArray(seriesData)) {
                y = seriesData;
            } else if (seriesData && Array.isArray(seriesData.y)) {
                y = seriesData.y;
                if (Array.isArray(seriesData.x)) x = seriesData.x;
            }
            if (!y.length || !x.length) return;
            const scaled = y.map((value) => convertLegValue(value, state.unit, leg, sibling));
            if (scaled.some((value) => value == null)) return;
            const baseSeries = { x: x.slice(), y: scaled };
            segmentSeries[year] = shouldAdjustMonthForLeg(leg, sibling)
                ? applyMonthAdjustmentToSeries(baseSeries, leg.month)
                : baseSeries;
        });
        if (Object.keys(segmentSeries).length) {
            referenceSegments.push({ reference: siblingReference, series: segmentSeries });
        }
        if (code === resolved.code) {
            vol30Series = commodityData.volatility_30d || commodityData.vol_30d || null;
        }
    });

    const merged = TRADE_MATH.mergeReferenceSeries(referenceSegments, targetReference);
    const series = {};
    Object.entries(merged).forEach(([year, yearSeries]) => {
        series[year] = normalizeLeapSeries(yearSeries, year);
    });

    if (!Object.keys(series).length) return null;
    return { leg, resolved, series, vol30Series };
}

function getLegSeriesFromEmbedded(leg) {
    const selection = parseMonthSelection(leg && leg.month ? leg.month : '');
    const components = selection ? DERIVED_PERIOD_COMPONENTS[selection.month] : null;
    if (!components) return getDirectLegSeriesFromEmbedded(leg);

    const componentItems = components
        .map((month) => getDirectLegSeriesFromEmbedded({
            ...leg,
            month: formatMonthOffset(month, selection.offset || 0)
        }))
        .filter(Boolean);
    if (componentItems.length !== components.length) return null;

    const yearSets = componentItems.map((item) => new Set(Object.keys(item.series)));
    const sharedYears = Array.from(yearSets[0]).filter(
        (year) => yearSets.every((set) => set.has(year))
    );
    const series = {};
    sharedYears.forEach((year) => {
        const averaged = TRADE_MATH.combineWeightedSeries(
            componentItems.map((item) => ({
                series: item.series[year],
                ratio: 1 / componentItems.length,
                native_unit: state.unit
            })),
            state.unit,
            { maxSpanDays: TRADE_MATH.DEFAULT_INTERPOLATION_MAX_SPAN_DAYS }
        );
        if (averaged.x.length) {
            series[year] = { x: averaged.x, y: averaged.y };
        }
    });
    if (!Object.keys(series).length) return null;
    return {
        leg,
        resolved: resolveCommodityForLeg(leg),
        series,
        vol30Series: null
    };
}

function getVolumeSeriesFromEmbedded(leg) {
    const embedded = window.EMBEDDED_DATA;
    if (!embedded || !embedded.commodities || !embedded.meta) return null;
    const resolved = resolveCommodityForLeg(leg);
    if (!resolved) return null;
    const commodityData = embedded.commodities[resolved.code];
    if (!commodityData || !commodityData.volumes) return null;

    const yearX = embedded.meta.yearX || {};
    const series = {};

    Object.keys(commodityData.volumes).forEach((year) => {
        const seriesData = commodityData.volumes[year];
        if (!seriesData) return;
        let x = yearX[year] || [];
        let y = [];

        if (Array.isArray(seriesData)) {
            y = seriesData;
        } else if (seriesData && Array.isArray(seriesData.y)) {
            y = seriesData.y;
            if (Array.isArray(seriesData.x)) x = seriesData.x;
        }

        if (!y.length || !x.length) return;
        const cleaned = y.map((value) => (Number.isFinite(value) ? value : null));
        series[year] = normalizeLeapSeries({ x: x.slice(), y: cleaned }, year);
    });

    if (!Object.keys(series).length) return null;
    return { leg, resolved, series };
}

async function getLegSeriesFromApi(leg) {
    const resolved = resolveCommodityForLeg(leg);
    if (!resolved) return null;
    const nativeUnit = getNativeUnitForLeg(leg, resolved);
    if (!nativeUnit) return null;
    const response = await fetch(`/api/data?commodity=${encodeURIComponent(resolved.code)}&unit=${encodeURIComponent(nativeUnit)}&field=${encodeURIComponent(state.field)}`);
    const data = await response.json();
    if (!data || !data.series) return null;

    const series = {};
    Object.keys(data.series).forEach((year) => {
        const seriesData = data.series[year];
        if (!seriesData || !Array.isArray(seriesData.y) || !Array.isArray(seriesData.x)) return;
        const converted = seriesData.y.map((value) => convertLegValue(value, state.unit, leg, resolved));
        if (converted.some((value) => value == null)) return;
        const baseSeries = { x: seriesData.x.slice(), y: converted };
        const adjusted = shouldAdjustMonthForLeg(leg, resolved)
            ? applyMonthAdjustmentToSeries(baseSeries, leg.month)
            : baseSeries;
        series[year] = normalizeLeapSeries(adjusted, year);
    });

    if (!Object.keys(series).length) return null;
    return { leg, resolved, series };
}

function alignFlatCurveLegSeries(legSeries, legs) {
    const nonFlatItems = legSeries.filter((item) => !isFlatCurveLeg(item.leg, item.resolved));
    if (!nonFlatItems.length) return legSeries;
    const anchor = getContractAnchorConfigFromLegs(legs);
    if (!anchor || !Number.isFinite(anchor.rollMonthIndex)) return legSeries;
    const targetYears = Array.from(new Set(
        nonFlatItems.flatMap((item) => Object.keys(item.series).map(Number).filter(Number.isFinite))
    )).sort((a, b) => a - b);
    if (!targetYears.length) return legSeries;

    return legSeries.map((item) => {
        if (!isFlatCurveLeg(item.leg, item.resolved)) return item;
        return {
            ...item,
            series: TRADE_MATH.alignFlatCurveSeries(
                item.series,
                targetYears,
                anchor.rollMonthIndex
            )
        };
    });
}

async function getVolumeSeriesFromApi(leg) {
    const resolved = resolveCommodityForLeg(leg);
    if (!resolved) return null;
    const response = await fetch(`/api/data?commodity=${encodeURIComponent(resolved.code)}&unit=${encodeURIComponent(state.unit)}&field=${encodeURIComponent(state.field)}`);
    const data = await response.json();
    if (!data || !data.volumes) return null;

    const series = {};
    Object.keys(data.volumes).forEach((year) => {
        const seriesData = data.volumes[year];
        if (!seriesData || !Array.isArray(seriesData.y) || !Array.isArray(seriesData.x)) return;
        const cleaned = seriesData.y.map((value) => (Number.isFinite(value) ? value : null));
        series[year] = normalizeLeapSeries({ x: seriesData.x.slice(), y: cleaned }, year);
    });

    if (!Object.keys(series).length) return null;
    return { leg, resolved, series };
}

function combineLegSeries(legSeries) {
    if (!legSeries.length) return null;
    const yearSets = legSeries.map((item) => new Set(Object.keys(item.series)));
    const sharedYears = Array.from(yearSets[0]).filter((year) => yearSets.every((set) => set.has(year)));
    const combined = {};
    const fullHistorySeries = {};
    const axisEndByYear = {};
    const cycle = getContractAnchorConfigFromLegs(legSeries.map((item) => item.leg));

    sharedYears.forEach((year) => {
        const perLeg = legSeries.map((item) => item.series[year]).filter(Boolean);
        if (perLeg.length !== legSeries.length) return;
        const aligned = TRADE_MATH.combineWeightedSeries(
            legSeries.map((item, index) => ({
                series: perLeg[index],
                ratio: Number(item.leg.ratio),
                native_unit: state.unit
            })),
            state.unit,
            { maxSpanDays: TRADE_MATH.DEFAULT_INTERPOLATION_MAX_SPAN_DAYS }
        );
        if (!aligned.x.length) return;
        fullHistorySeries[year] = { x: aligned.x.slice(), y: aligned.y.slice() };
        const clipped = cycle
            ? TRADE_MATH.clipSeriesToSourceCycle(aligned, cycle.rollMonthIndex)
            : aligned;
        if (!clipped.x.length) return;
        const axisEndDay = TRADE_MATH.latestContractEndDay([clipped]);
        if (Number.isFinite(axisEndDay)) {
            axisEndByYear[year] = axisEndDay;
        }
        combined[year] = { x: clipped.x, y: clipped.y };
    });

    if (!Object.keys(combined).length) return null;
    const latestYear = Math.max(...Object.keys(combined).map(Number));
    const latestSeries = fullHistorySeries[latestYear] || combined[latestYear];
    const varStats = latestSeries
        ? calculateVarStats(latestSeries.y.filter(Number.isFinite))
        : { p90: 0, p95: 0, p99: 0 };
    return {
        series: combined,
        fullHistorySeries,
        axisEndByYear,
        var: varStats,
        interpolation: {
            method: 'linear-interior',
            endpointExtrapolation: false
        }
    };
}

function hasLegMonth(leg) {
    return Boolean(leg && typeof leg.month === 'string' && leg.month.trim());
}

function hasRequiredLegMonth(leg) {
    return isFlatCurveLeg(leg) || hasLegMonth(leg);
}

function getLegsForCalculation() {
    const legs = getLegsWithEffectiveRatios().filter((leg) => {
        const ratio = Number(leg.ratio);
        return leg.code && Number.isFinite(ratio) && ratio !== 0;
    });
    if (!legs.length) return [];
    if (legs.some((leg) => !hasRequiredLegMonth(leg))) {
        return [];
    }
    return legs;
}

function getMissingLegCodes(legs) {
    const missing = new Set();
    legs.forEach((leg) => {
        if (!resolveCommodityForLeg(leg)) {
            missing.add(leg.code);
        }
    });
    return Array.from(missing);
}

function getLegConfigurationIssues(legs) {
    const issues = [];
    (legs || []).forEach((leg) => {
        const resolved = resolveCommodityForLeg(leg);
        const root = getRootCodeFromLeg(leg, resolved);
        const config = getRootConfig(root);
        if (!config) return;
        if (!TRADE_MATH.normalizeUnit(config.native_unit)) {
            issues.push(`${root}: set native_unit to cpg, $/gal, $/bbl, or $/MT`);
            return;
        }
        const bblPerMT = Number(config.bbl_per_mt);
        const galPerBbl = Number(config.gal_per_bbl);
        if (!Number.isFinite(bblPerMT) || bblPerMT <= 0) {
            issues.push(`${root}: bbl_per_mt must be greater than zero`);
        }
        if (!Number.isFinite(galPerBbl) || galPerBbl <= 0) {
            issues.push(`${root}: gal_per_bbl must be greater than zero`);
        }
    });
    return Array.from(new Set(issues));
}

function combineVolumeSeries(legSeries) {
    if (!legSeries.length) return null;
    const yearSets = legSeries.map((item) => new Set(Object.keys(item.series)));
    const sharedYears = Array.from(yearSets[0]).filter((year) => yearSets.every((set) => set.has(year)));
    const combined = {};

    sharedYears.forEach((year) => {
        const perLeg = legSeries.map((item) => item.series[year]).filter(Boolean);
        if (!perLeg.length) return;
        const xSets = perLeg.map((series) => {
            const set = new Set();
            if (!series || !Array.isArray(series.x)) return set;
            series.x.forEach((value) => {
                if (Number.isFinite(value)) set.add(value);
            });
            return set;
        });
        if (!xSets.length) return;
        let intersection = xSets[0];
        for (let i = 1; i < xSets.length; i++) {
            intersection = new Set(Array.from(intersection).filter((val) => xSets[i].has(val)));
        }
        const x = Array.from(intersection).sort((a, b) => a - b);
        if (!x.length) return;
        const lookups = legSeries.map((item) => buildSeriesLookup(item.series[year]));
        const y = x.map((xVal) => {
            let sum = 0;
            let count = 0;
            for (let i = 0; i < legSeries.length; i++) {
                const value = lookups[i].get(String(xVal));
                if (!Number.isFinite(value)) continue;
                sum += value;
                count += 1;
            }
            return count ? TRADE_MATH.round5(sum) : null;
        });
        combined[year] = { x, y };
    });

    if (!Object.keys(combined).length) return null;
    return { series: combined };
}

function getLegsForVolume() {
    const legs = getActiveLegs().filter((leg) => leg.code);
    if (!legs.length) return [];
    if (legs.some((leg) => !hasRequiredLegMonth(leg))) {
        return [];
    }
    return legs;
}

async function getCombinedSeriesData(legsOverride) {
    const legs = legsOverride || getLegsForCalculation();
    if (!legs.length) return null;

    const embedded = window.EMBEDDED_DATA;
    if (embedded && embedded.commodities && embedded.meta) {
        let legSeries = legs
            .map((leg) => getLegSeriesFromEmbedded(leg))
            .filter(Boolean)
            .map(rebaseLegSeriesToFormulaYears);
        if (legSeries.length !== legs.length) return null;
        legSeries = alignFlatCurveLegSeries(legSeries, legs);
        const combined = combineLegSeries(legSeries);
        if (combined) {
            if (legs.length === 1 && legSeries[0].vol30Series) {
                combined.vol30 = legSeries[0].vol30Series;
            } else if (legSeries.every((item) => item.vol30Series)) {
                combined.vol30Legs = legSeries;
            }
        }
        return combined;
    }

    const legSeries = await Promise.all(legs.map((leg) => getLegSeriesFromApi(leg)));
    const filtered = alignFlatCurveLegSeries(
        legSeries.filter(Boolean).map(rebaseLegSeriesToFormulaYears),
        legs
    );
    if (filtered.length !== legs.length) return null;
    return combineLegSeries(filtered);
}

async function getCombinedVolumeSeriesData(legsOverride) {
    const legs = legsOverride || getLegsForVolume();
    if (!legs.length) return null;

    const embedded = window.EMBEDDED_DATA;
    if (embedded && embedded.commodities && embedded.meta) {
        const legSeries = legs
            .map((leg) => getVolumeSeriesFromEmbedded(leg))
            .filter(Boolean)
            .map(rebaseLegSeriesToFormulaYears);
        if (!legSeries.length) return null;
        return combineVolumeSeries(legSeries);
    }

    const legSeries = await Promise.all(legs.map((leg) => getVolumeSeriesFromApi(leg)));
    const filtered = legSeries.filter(Boolean).map(rebaseLegSeriesToFormulaYears);
    if (!filtered.length) return null;
    return combineVolumeSeries(filtered);
}

function calculateVarStats(values) {
    if (!values || values.length < 60) {
        return { p90: 0, p95: 0, p99: 0 };
    }

    const recent = values.slice(-60);
    const hasNonPositive = recent.some(v => v <= 0);
    let vol = 0;

    if (hasNonPositive) {
        const diffs = [];
        for (let i = 1; i < recent.length; i++) {
            diffs.push(recent[i] - recent[i - 1]);
        }
        vol = standardDeviation(diffs);
        return {
            p90: vol * 1.645,
            p95: vol * 1.96,
            p99: vol * 2.576
        };
    }

    const returns = [];
    for (let i = 1; i < recent.length; i++) {
        returns.push(Math.log(recent[i] / recent[i - 1]));
    }
    vol = standardDeviation(returns);
    const lastPrice = recent[recent.length - 1];

    return {
        p90: lastPrice * vol * 1.645,
        p95: lastPrice * vol * 1.96,
        p99: lastPrice * vol * 2.576
    };
}

function normalizeVolValue(value) {
    if (!Number.isFinite(value)) return null;
    return value > 1 ? value / 100 : value;
}

function extractSeriesValues(series) {
    if (!series) return [];
    if (Array.isArray(series)) return series;
    if (Array.isArray(series.y)) return series.y;
    return [];
}

function resolveSeriesForYear(seriesMap, year) {
    if (!seriesMap) return null;
    return seriesMap[year] || seriesMap[String(year)] || null;
}

function getVarStatsFromVolSeries(priceSeries, volSeries) {
    const prices = extractSeriesValues(priceSeries);
    const vols = extractSeriesValues(volSeries);
    if (!prices.length || !vols.length) return null;
    const lastPrice = prices[prices.length - 1];
    const lastVolRaw = vols[vols.length - 1];
    const vol = normalizeVolValue(lastVolRaw);
    if (!Number.isFinite(lastPrice) || !Number.isFinite(vol)) return null;
    return {
        p90: lastPrice * vol * 1.645,
        p95: lastPrice * vol * 1.96,
        p99: lastPrice * vol * 2.576
    };
}

function getVarStatsFromLegVol(legSeries, year) {
    if (!Array.isArray(legSeries) || !legSeries.length || !Number.isFinite(year)) return null;
    const xSets = [];
    const lookups = [];
    const ratios = [];
    legSeries.forEach((item) => {
        if (!item || !item.series || !item.vol30Series) return;
        const ratio = Number(item.leg && item.leg.ratio);
        if (!Number.isFinite(ratio) || ratio === 0) return;
        const priceSeries = resolveSeriesForYear(item.series, year);
        const volSeries = resolveSeriesForYear(item.vol30Series, year);
        if (!priceSeries || !volSeries || !Array.isArray(priceSeries.x) || !Array.isArray(volSeries.x)) return;
        const priceLookup = buildSeriesLookup(priceSeries);
        const volLookup = buildSeriesLookup(volSeries);
        const xSet = new Set();
        priceSeries.x.forEach((value) => {
            if (Number.isFinite(value) && volLookup.has(String(value))) {
                xSet.add(value);
            }
        });
        if (!xSet.size) return;
        xSets.push(xSet);
        lookups.push({ priceLookup, volLookup });
        ratios.push(ratio);
    });
    if (!xSets.length) return null;
    let intersection = xSets[0];
    for (let i = 1; i < xSets.length; i++) {
        intersection = new Set(Array.from(intersection).filter((val) => xSets[i].has(val)));
    }
    if (!intersection.size) return null;
    const latestX = Math.max(...Array.from(intersection));
    let variance = 0;
    for (let i = 0; i < lookups.length; i++) {
        const price = lookups[i].priceLookup.get(String(latestX));
        const volRaw = lookups[i].volLookup.get(String(latestX));
        const vol = normalizeVolValue(volRaw);
        if (!Number.isFinite(price) || !Number.isFinite(vol)) continue;
        const legSigma = ratios[i] * price * vol;
        variance += legSigma * legSigma;
    }
    if (!Number.isFinite(variance) || variance <= 0) return null;
    const sigma = Math.sqrt(variance);
    return {
        p90: sigma * 1.645,
        p95: sigma * 1.96,
        p99: sigma * 2.576
    };
}

function getVarStatsForData(data) {
    if (!data || !data.series) return { p90: 0, p95: 0, p99: 0 };
    const years = Object.keys(data.series).map(Number).filter(Number.isFinite);
    if (!years.length) return { p90: 0, p95: 0, p99: 0 };
    const activeYears = resolveActiveYears(years);
    const latestYear = activeYears.length ? Math.max(...activeYears) : Math.max(...years);
    if (data.vol30Legs) {
        const stats = getVarStatsFromLegVol(data.vol30Legs, latestYear);
        if (stats) return stats;
    }
    if (data.vol30) {
        const volSeries = data.vol30[latestYear] || data.vol30[String(latestYear)];
        const priceSeries = data.series[latestYear] || data.series[String(latestYear)];
        const stats = getVarStatsFromVolSeries(priceSeries, volSeries);
        if (stats) return stats;
    }
    const fullHistory = resolveSeriesForYear(data.fullHistorySeries, latestYear);
    if (fullHistory && Array.isArray(fullHistory.y)) {
        return calculateVarStats(fullHistory.y.filter(Number.isFinite));
    }
    if (data.var) return data.var;
    const series = data.series[latestYear] || data.series[String(latestYear)];
    return calculateVarStats(series && series.y ? series.y : []);
}

function resolveSeriesXY(seriesMap, year) {
    const series = resolveSeriesForYear(seriesMap, year);
    if (!series) return null;
    if (Array.isArray(series)) {
        return { x: null, y: series };
    }
    if (Array.isArray(series.x) && Array.isArray(series.y)) {
        return { x: series.x, y: series.y };
    }
    if (Array.isArray(series.y)) {
        return { x: series.x || null, y: series.y };
    }
    return null;
}

function buildVarSeriesFromVol(priceXY, volXY, multiplier) {
    if (!priceXY || !volXY) return null;
    const priceX = Array.isArray(priceXY.x) ? priceXY.x : [];
    const priceY = Array.isArray(priceXY.y) ? priceXY.y : [];
    const volX = Array.isArray(volXY.x) ? volXY.x : priceX;
    const volY = Array.isArray(volXY.y) ? volXY.y : [];
    if (!priceX.length || !priceY.length || !volY.length) return null;
    const priceLookup = buildSeriesLookup({ x: priceX, y: priceY });
    const volLookup = buildSeriesLookup({ x: volX, y: volY });
    const xSet = new Set(priceX.filter(Number.isFinite));
    const x = Array.from(xSet).filter((value) => volLookup.has(String(value))).sort((a, b) => a - b);
    if (!x.length) return null;
    const y = x.map((xVal) => {
        const price = priceLookup.get(String(xVal));
        const volRaw = volLookup.get(String(xVal));
        const vol = normalizeVolValue(volRaw);
        if (!Number.isFinite(price) || !Number.isFinite(vol)) return null;
        return TRADE_MATH.round5(price * vol * multiplier);
    });
    return { x, y };
}

function buildVarSeriesFromLegVol(legSeries, year, multiplier) {
    if (!Array.isArray(legSeries) || !legSeries.length) return null;
    const legMaps = [];
    const xSets = [];

    for (const item of legSeries) {
        if (!item || !item.series || !item.vol30Series) return null;
        const ratio = Number(item.leg && item.leg.ratio);
        if (!Number.isFinite(ratio) || ratio === 0) return null;
        const priceXY = resolveSeriesXY(item.series, year);
        const volXY = resolveSeriesXY(item.vol30Series, year);
        if (!priceXY || !volXY) return null;
        const priceX = Array.isArray(priceXY.x) ? priceXY.x : [];
        const priceY = Array.isArray(priceXY.y) ? priceXY.y : [];
        const volX = Array.isArray(volXY.x) ? volXY.x : priceX;
        const volY = Array.isArray(volXY.y) ? volXY.y : [];
        if (!priceX.length || !priceY.length || !volY.length) return null;
        const priceLookup = buildSeriesLookup({ x: priceX, y: priceY });
        const volLookup = buildSeriesLookup({ x: volX, y: volY });
        const xSet = new Set(priceX.filter(Number.isFinite).filter((value) => volLookup.has(String(value))));
        legMaps.push({ ratio, priceLookup, volLookup });
        xSets.push(xSet);
    }

    if (!xSets.length) return null;
    let intersection = xSets[0];
    for (let i = 1; i < xSets.length; i++) {
        intersection = new Set(Array.from(intersection).filter((val) => xSets[i].has(val)));
    }
    const x = Array.from(intersection).sort((a, b) => a - b);
    if (!x.length) return null;
    const y = x.map((xVal) => {
        let variance = 0;
        for (const leg of legMaps) {
            const price = leg.priceLookup.get(String(xVal));
            const volRaw = leg.volLookup.get(String(xVal));
            const vol = normalizeVolValue(volRaw);
            if (!Number.isFinite(price) || !Number.isFinite(vol)) return null;
            const sigma = leg.ratio * price * vol;
            variance += sigma * sigma;
        }
        if (!Number.isFinite(variance) || variance <= 0) return null;
        return TRADE_MATH.round5(Math.sqrt(variance) * multiplier);
    });
    return { x, y };
}

function calculateVolatilityHistogram(data) {
    if (!data || !data.series) return null;
    const years = Object.keys(data.series).map(Number).filter(Number.isFinite).sort((a, b) => a - b);
    const activeYears = resolveActiveYears(years);
    if (!activeYears.length) return null;

    const counts = new Map();
    let minIdx = Infinity;
    let maxIdx = -Infinity;
    let total = 0;

    activeYears.forEach((year) => {
        const seriesData = data.series[year];
        if (!seriesData || !Array.isArray(seriesData.y)) return;
        const values = seriesData.y;
        for (let i = 1; i < values.length; i++) {
            const prev = values[i - 1];
            const current = values[i];
            if (!Number.isFinite(prev) || !Number.isFinite(current) || prev === 0) continue;
            const pct = ((current - prev) / Math.abs(prev)) * 100;
            if (!Number.isFinite(pct)) continue;
            const idx = Math.floor((pct + 2.5) / 5);
            counts.set(idx, (counts.get(idx) || 0) + 1);
            minIdx = Math.min(minIdx, idx);
            maxIdx = Math.max(maxIdx, idx);
            total += 1;
        }
    });

    if (!total || !Number.isFinite(minIdx) || !Number.isFinite(maxIdx)) return null;

    const bins = [];
    const values = [];
    for (let idx = minIdx; idx <= maxIdx; idx++) {
        bins.push(idx * 5);
        values.push(((counts.get(idx) || 0) / total) * 100);
    }

    return { bins, values, total };
}

function calculateVarSeasonalitySeries(data, windowSize = 60, multiplier = 1.645) {
    if (!data || !data.series) return null;
    const years = Object.keys(data.series).map(Number).filter(Number.isFinite).sort((a, b) => a - b);
    if (!years.length) return null;
    const series = {};

    if (data.vol30Legs) {
        years.forEach((year) => {
            const varSeries = buildVarSeriesFromLegVol(data.vol30Legs, year, multiplier);
            if (varSeries && varSeries.y.some((value) => Number.isFinite(value))) {
                series[year] = { x: varSeries.x.slice(), y: varSeries.y.slice() };
            }
        });
        if (Object.keys(series).length) {
            return { series, level: 90 };
        }
    }

    if (data.vol30) {
        years.forEach((year) => {
            const priceXY = resolveSeriesXY(data.series, year);
            const volXY = resolveSeriesXY(data.vol30, year);
            const varSeries = buildVarSeriesFromVol(priceXY, volXY, multiplier);
            if (varSeries && varSeries.y.some((value) => Number.isFinite(value))) {
                series[year] = { x: varSeries.x.slice(), y: varSeries.y.slice() };
            }
        });
        if (Object.keys(series).length) {
            return { series, level: 90 };
        }
    }

    years.forEach((year) => {
        const plottedSeriesData = resolveSeriesForYear(data.series, year);
        const fullHistory = resolveSeriesForYear(data.fullHistorySeries, year);
        const seriesData = fullHistory || plottedSeriesData;
        if (!seriesData || !Array.isArray(seriesData.y) || !Array.isArray(seriesData.x)) return;
        const values = seriesData.y;
        const xValues = seriesData.x;

        if (data.vol30Legs) {
            const varSeries = buildVarSeasonalityFromLegVol(data.vol30Legs, year, multiplier);
            if (varSeries) {
                series[year] = varSeries;
            }
            return;
        }

        if (data.vol30) {
            const volSeries = resolveSeriesForYear(data.vol30, year);
            const varSeries = buildVarSeasonalityFromVol(seriesData, volSeries, multiplier);
            if (varSeries) {
                series[year] = varSeries;
            }
            return;
        }

        if (values.length < windowSize + 1) return;

        const hasNonPositive = values.some((value) => value <= 0);
        const returns = new Array(values.length).fill(null);
        for (let i = 1; i < values.length; i++) {
            const prev = values[i - 1];
            const current = values[i];
            if (!Number.isFinite(prev) || !Number.isFinite(current)) continue;
            if (hasNonPositive) {
                returns[i] = current - prev;
            } else if (prev > 0 && current > 0) {
                returns[i] = Math.log(current / prev);
            }
        }

        const varValues = new Array(values.length).fill(null);
        let queue = [];
        let sum = 0;
        let sumSq = 0;

        for (let i = 1; i < values.length; i++) {
            const r = returns[i];
            if (!Number.isFinite(r)) {
                queue = [];
                sum = 0;
                sumSq = 0;
                continue;
            }
            queue.push(r);
            sum += r;
            sumSq += r * r;
            if (queue.length > windowSize) {
                const removed = queue.shift();
                sum -= removed;
                sumSq -= removed * removed;
            }
            if (queue.length === windowSize) {
                const mean = sum / windowSize;
                const variance = Math.max(sumSq / windowSize - mean ** 2, 0);
                const vol = Math.sqrt(variance);
                const baseValue = values[i];
                const varValue = hasNonPositive ? vol * multiplier : baseValue * vol * multiplier;
                if (Number.isFinite(varValue)) {
                    varValues[i] = TRADE_MATH.round5(varValue);
                }
            }
        }

        let outputX = xValues.slice();
        let outputY = varValues;
        if (fullHistory && plottedSeriesData && Array.isArray(plottedSeriesData.x)) {
            const plottedX = new Set(plottedSeriesData.x.map(String));
            outputX = [];
            outputY = [];
            xValues.forEach((xValue, index) => {
                if (!plottedX.has(String(xValue))) return;
                outputX.push(xValue);
                outputY.push(varValues[index]);
            });
        }
        const hasValues = outputY.some((value) => Number.isFinite(value));
        if (!hasValues) return;
        series[year] = { x: outputX, y: outputY };
    });

    if (!Object.keys(series).length) return null;
    return { series, level: 90 };
}

function buildVarSeasonalityFromVol(priceSeries, volSeries, multiplier) {
    if (!priceSeries || !volSeries) return null;
    if (!Array.isArray(priceSeries.x) || !Array.isArray(priceSeries.y)) return null;
    if (!Array.isArray(volSeries.x) || !Array.isArray(volSeries.y)) return null;
    const priceLookup = buildSeriesLookup(priceSeries);
    const volLookup = buildSeriesLookup(volSeries);
    const x = priceSeries.x.filter((value) => Number.isFinite(value) && volLookup.has(String(value)));
    if (!x.length) return null;
    const y = x.map((xVal) => {
        const price = priceLookup.get(String(xVal));
        const volRaw = volLookup.get(String(xVal));
        const vol = normalizeVolValue(volRaw);
        if (!Number.isFinite(price) || !Number.isFinite(vol)) return null;
        return TRADE_MATH.round5(price * vol * multiplier);
    });
    if (!y.some((value) => Number.isFinite(value))) return null;
    return { x, y };
}

function buildVarSeasonalityFromLegVol(legSeries, year, multiplier) {
    if (!Array.isArray(legSeries) || !legSeries.length) return null;
    const xSets = [];
    const lookups = [];
    const ratios = [];
    legSeries.forEach((item) => {
        if (!item || !item.series || !item.vol30Series) return;
        const ratio = Number(item.leg && item.leg.ratio);
        if (!Number.isFinite(ratio) || ratio === 0) return;
        const priceSeries = resolveSeriesForYear(item.series, year);
        const volSeries = resolveSeriesForYear(item.vol30Series, year);
        if (!priceSeries || !volSeries || !Array.isArray(priceSeries.x) || !Array.isArray(volSeries.x)) return;
        const priceLookup = buildSeriesLookup(priceSeries);
        const volLookup = buildSeriesLookup(volSeries);
        const xSet = new Set();
        priceSeries.x.forEach((value) => {
            if (Number.isFinite(value) && volLookup.has(String(value))) {
                xSet.add(value);
            }
        });
        if (!xSet.size) return;
        xSets.push(xSet);
        lookups.push({ priceLookup, volLookup });
        ratios.push(ratio);
    });
    if (!xSets.length) return null;
    let intersection = xSets[0];
    for (let i = 1; i < xSets.length; i++) {
        intersection = new Set(Array.from(intersection).filter((val) => xSets[i].has(val)));
    }
    const x = Array.from(intersection).sort((a, b) => a - b);
    if (!x.length) return null;
    const y = x.map((xVal) => {
        let variance = 0;
        for (let i = 0; i < lookups.length; i++) {
            const price = lookups[i].priceLookup.get(String(xVal));
            const volRaw = lookups[i].volLookup.get(String(xVal));
            const vol = normalizeVolValue(volRaw);
            if (!Number.isFinite(price) || !Number.isFinite(vol)) return null;
            const legSigma = ratios[i] * price * vol;
            variance += legSigma * legSigma;
        }
        if (!Number.isFinite(variance) || variance <= 0) return null;
        return TRADE_MATH.round5(Math.sqrt(variance) * multiplier);
    });
    if (!y.some((value) => Number.isFinite(value))) return null;
    return { x, y };
}


function standardDeviation(values) {
    if (!values.length) return 0;
    const mean = values.reduce((sum, v) => sum + v, 0) / values.length;
    const variance = values.reduce((sum, v) => sum + (v - mean) ** 2, 0) / values.length;
    return Math.sqrt(variance);
}

function calculateSMA(values, period) {
    const result = Array(values.length).fill(null);
    if (values.length < period) return result;
    let sum = 0;
    for (let i = 0; i < values.length; i++) {
        sum += values[i];
        if (i >= period) {
            sum -= values[i - period];
        }
        if (i >= period - 1) {
            result[i] = sum / period;
        }
    }
    return result;
}

function calculateEMA(values, period) {
    const result = Array(values.length).fill(null);
    if (values.length < period) return result;
    let sum = 0;
    for (let i = 0; i < period; i++) {
        sum += values[i];
    }
    let ema = sum / period;
    result[period - 1] = ema;
    const k = 2 / (period + 1);
    for (let i = period; i < values.length; i++) {
        ema = values[i] * k + ema * (1 - k);
        result[i] = ema;
    }
    return result;
}

function calculateEMAForSeries(values, period) {
    const result = Array(values.length).fill(null);
    const valid = values
        .map((value, index) => ({ value, index }))
        .filter((entry) => typeof entry.value === 'number' && !Number.isNaN(entry.value));
    if (valid.length < period) return result;

    let sum = 0;
    for (let i = 0; i < period; i++) {
        sum += valid[i].value;
    }
    let ema = sum / period;
    result[valid[period - 1].index] = ema;
    const k = 2 / (period + 1);

    for (let i = period; i < valid.length; i++) {
        ema = valid[i].value * k + ema * (1 - k);
        result[valid[i].index] = ema;
    }
    return result;
}

function calculateBollinger(values, period, multiplier) {
    const mid = Array(values.length).fill(null);
    const upper = Array(values.length).fill(null);
    const lower = Array(values.length).fill(null);
    if (values.length < period) return { mid, upper, lower };

    let sum = 0;
    let sumSq = 0;
    for (let i = 0; i < values.length; i++) {
        sum += values[i];
        sumSq += values[i] ** 2;
        if (i >= period) {
            sum -= values[i - period];
            sumSq -= values[i - period] ** 2;
        }
        if (i >= period - 1) {
            const mean = sum / period;
            const variance = Math.max(sumSq / period - mean ** 2, 0);
            const std = Math.sqrt(variance);
            mid[i] = mean;
            upper[i] = mean + multiplier * std;
            lower[i] = mean - multiplier * std;
        }
    }

    return { mid, upper, lower };
}

function calculateRSI(values, period) {
    const result = Array(values.length).fill(null);
    if (values.length <= period) return result;

    let gainSum = 0;
    let lossSum = 0;
    for (let i = 1; i <= period; i++) {
        const diff = values[i] - values[i - 1];
        if (diff >= 0) {
            gainSum += diff;
        } else {
            lossSum += Math.abs(diff);
        }
    }

    let avgGain = gainSum / period;
    let avgLoss = lossSum / period;
    result[period] = avgLoss === 0 ? 100 : 100 - (100 / (1 + avgGain / avgLoss));

    for (let i = period + 1; i < values.length; i++) {
        const diff = values[i] - values[i - 1];
        const gain = diff > 0 ? diff : 0;
        const loss = diff < 0 ? Math.abs(diff) : 0;

        avgGain = (avgGain * (period - 1) + gain) / period;
        avgLoss = (avgLoss * (period - 1) + loss) / period;

        if (avgLoss === 0) {
            result[i] = 100;
        } else {
            const rs = avgGain / avgLoss;
            result[i] = 100 - (100 / (1 + rs));
        }
    }

    return result;
}

function calculateMACD(values, fastPeriod, slowPeriod, signalPeriod) {
    const emaFast = calculateEMA(values, fastPeriod);
    const emaSlow = calculateEMA(values, slowPeriod);
    const macd = values.map((_, index) => {
        if (emaFast[index] == null || emaSlow[index] == null) return null;
        return emaFast[index] - emaSlow[index];
    });
    const signal = calculateEMAForSeries(macd, signalPeriod);
    const histogram = macd.map((value, index) => {
        if (value == null || signal[index] == null) return null;
        return value - signal[index];
    });
    return { macd, signal, histogram };
}

function getCommodityLabel(code) {
    const commodities = getCommodityList();
    const match = commodities.find((com) => com.code === code || com.root_code === code || com.security === code);
    if (!match) return code;
    return match.clean_name || match.name || match.code || code;
}

function buildDateLabels(values) {
    if (!Array.isArray(values)) return [];
    const baseYear = 2000; // leap year for consistent Feb 29 mapping
    return values.map((value) => {
        if (!Number.isFinite(value)) return '';
        const day = TRADE_MATH.sourceCycleDay(Math.round(value));
        const date = new Date(baseYear, 0, 1);
        date.setDate(day);
        return date.toLocaleDateString(undefined, { month: 'short', day: '2-digit' });
    });
}

function formatContractCycleDay(value) {
    if (!Number.isFinite(value)) return '';
    const day = Math.max(1, Math.min(ROLLING_AXIS_SPAN, Math.round(value)));
    const date = new Date(2001, 0, 1);
    date.setDate(day);
    return date.toLocaleDateString(undefined, { month: 'short', day: '2-digit' });
}

function setContractAxisDiagnostics(container, traces, anchorDay, span, highlightName) {
    if (!container || !Number.isFinite(anchorDay) || !Number.isFinite(span)) return;
    const normalizedAnchor = ((anchorDay - 1) % span + span) % span + 1;
    const startDay = normalizedAnchor >= span ? 1 : normalizedAnchor + 1;
    const highlightTrace = (traces || []).find((trace) => String(trace.name) === String(highlightName));
    const highlightX = highlightTrace && Array.isArray(highlightTrace.x)
        ? highlightTrace.x.filter(Number.isFinite)
        : [];
    container.dataset.contractAxisRange = `1:${span}`;
    container.dataset.contractAxisSpan = String(span);
    container.dataset.contractAxisAnchorDay = String(normalizedAnchor);
    container.dataset.contractAxisStartLabel = formatContractCycleDay(startDay);
    container.dataset.contractAxisEndLabel = formatContractCycleDay(normalizedAnchor);
    container.dataset.highlightYear = String(highlightName);
    container.dataset.highlightFirstX = highlightX.length ? String(highlightX[0]) : '';
    container.dataset.highlightLastX = highlightX.length ? String(highlightX[highlightX.length - 1]) : '';
}

function normalizeLeapSeries(series, year) {
    if (!series || !Array.isArray(series.x) || !Array.isArray(series.y)) return series;
    const length = Math.min(series.x.length, series.y.length);
    const x = series.x.slice(0, length);
    const y = series.y.slice(0, length);
    let lastValue = null;
    for (let i = 0; i < length; i++) {
        const value = x[i];
        if (!Number.isFinite(value)) continue;
        if (lastValue != null && value < lastValue) {
            return { x, y };
        }
        lastValue = value;
    }
    const continuous = TRADE_MATH.interpolateInteriorSeries(
        { x, y },
        TRADE_MATH.DEFAULT_INTERPOLATION_MAX_SPAN_DAYS
    );
    return { x: continuous.x, y: continuous.y };
}

// CHARTING
function resetPlot(message) {
    const plot = document.getElementById('plotly-div');
    if (plot) {
        Plotly.purge(plot);
        plot.innerHTML = '';
    }
    setChartEmpty(true, message);
    lastVolatilityHistogram = null;
    lastVarSeasonality = null;
    const overlayTitle = document.getElementById('overlay-title');
    if (overlayTitle) {
        overlayTitle.innerText = '';
    }
    const chartTitle = document.getElementById('chart-title');
    if (chartTitle) {
        chartTitle.innerText = '';
    }
    const var90 = document.getElementById('var-90');
    const var95 = document.getElementById('var-95');
    const var99 = document.getElementById('var-99');
    const varYearLabel = document.getElementById('var-year-label');
    if (var90) var90.innerText = '--';
    if (var95) var95.innerText = '--';
    if (var99) var99.innerText = '--';
    if (varYearLabel) {
        varYearLabel.innerText = '';
        varYearLabel.style.display = 'none';
    }
    const histogram = document.getElementById('var-histogram');
    if (histogram) {
        histogram.classList.add('is-hidden');
    }
    const histogramPlot = document.getElementById('var-histogram-plot');
    if (histogramPlot) {
        if (window.Plotly) {
            Plotly.purge(histogramPlot);
        }
        histogramPlot.innerHTML = '';
    }
    const varSeasonalityPlot = document.getElementById('var-seasonality-plot');
    if (varSeasonalityPlot) {
        if (window.Plotly) {
            Plotly.purge(varSeasonalityPlot);
        }
        varSeasonalityPlot.innerHTML = '';
    }
    const volumePlot = document.getElementById('volume-seasonality-plot');
    if (volumePlot) {
        if (window.Plotly) {
            Plotly.purge(volumePlot);
        }
        volumePlot.innerHTML = '';
    }
}

function setChartEmpty(isEmpty, message) {
    const empty = document.getElementById('chart-empty');
    if (!empty) return;
    empty.textContent = message || 'Select a code to begin';
    empty.classList.toggle('is-hidden', !isEmpty);
}

async function updateChart() {
    try {
        const legs = getLegsForCalculation();
        if (!legs.length) {
            const activeLegs = getLegsWithEffectiveRatios().filter((leg) => {
                const ratio = Number(leg.ratio);
                return leg.code && Number.isFinite(ratio) && ratio !== 0;
            });
            const missingMonth = activeLegs.length && activeLegs.some((leg) => !hasRequiredLegMonth(leg));
            resetPlot(missingMonth ? 'Select a month to begin' : undefined);
            renderVarSeasonalityChart(null);
            return;
        }

        const missing = getMissingLegCodes(legs);
        if (missing.length) {
            resetPlot(`Missing data for: ${missing.join(', ')}`);
            renderVarSeasonalityChart(null);
            return;
        }

        const configurationIssues = getLegConfigurationIssues(legs);
        if (configurationIssues.length) {
            resetPlot(`Root configuration: ${configurationIssues.join('; ')}`);
            renderVarSeasonalityChart(null);
            return;
        }

        const combined = await getCombinedSeriesData(legs);
        if (!combined) {
            if (state.field !== 'last') {
                setField('last');
                return;
            }
            resetPlot();
            renderVarSeasonalityChart(null);
            return;
        }
        if (!seriesMapHasValues(combined.series)) {
            if (state.field !== 'last') {
                setField('last');
                return;
            }
            resetPlot('No data for selected field');
            renderVarSeasonalityChart(null);
            return;
        }
        lastRenderedData = combined;
        setChartEmpty(false);
        renderPlot(combined);
        renderVarSeasonalityChart(combined);
    } catch (e) {
        console.error(e);
    } finally {
        schedulePlotlyResize();
    }
}

function renderPlot(data) {
    const traces = [];
    const palette = getThemePalette();
    const colors = getSeriesColors();

    const sortedYears = Object.keys(data.series).map(Number).sort((a, b) => a - b);
    const activeYears = resolveActiveYears(sortedYears);
    const axisSpan = ROLLING_AXIS_SPAN;
    const anchorDay = getContractAxisAnchorDay(data, activeYears);
    const useRollingAxis = Number.isFinite(anchorDay);
    const displayRange = [1, axisSpan];
    let colorIdx = 0;
    let xMin = null;
    let xMax = null;
    let yMin = null;
    let yMax = null;

    const highlightYear = activeYears.length ? Math.max(...activeYears) : getLatestYear();
    const displayHighlightYear = getDisplayYear(highlightYear);
    activeYears.forEach(year => {
        if (year === highlightYear) return;

        const seriesData = data.series[year];
        if (!seriesData) return;
        const dateLabels = buildDateLabels(seriesData.x);
        let plotX = seriesData.x;
        let plotY = seriesData.y;
        let plotLabels = dateLabels;
        if (useRollingAxis) {
            const normalized = normalizeSeriesForRollingAxis(plotX, plotY, plotLabels);
            plotX = normalized.x;
            plotY = normalized.y;
            plotLabels = normalized.labels || plotLabels;
            plotX = shiftSeriesX(plotX, anchorDay, axisSpan);
            const clipped = clipSeriesToSpan(plotX, plotY, plotLabels, axisSpan);
            plotX = clipped.x;
            plotY = clipped.y;
            plotLabels = clipped.labels || plotLabels;
            const sorted = sortSeriesByX(plotX, plotY, plotLabels);
            plotX = TRADE_MATH.alignCycleStart(sorted.x, 1);
            plotY = sorted.y;
            plotLabels = sorted.customdata || plotLabels;
        }
        const continuous = keepLongestContinuousPlotSeries(plotX, plotY, plotLabels);
        plotX = continuous.x;
        plotY = continuous.y;
        plotLabels = continuous.labels || plotLabels;
        const contractLabel = getHoverContractLabel(year);
        const formulaLabel = buildContractFormulaForYear(year);
        const customdata = buildHoverCustomdata(plotLabels, contractLabel, formulaLabel);
        const hovertemplate = formulaLabel
            ? '%{customdata[0]} · %{y}<br>%{customdata[1]}<br>%{customdata[2]}<extra>%{fullData.name}</extra>'
            : (contractLabel
                ? '%{customdata[0]} · %{y}<br>%{customdata[1]}<extra>%{fullData.name}</extra>'
                : '%{customdata[0]} · %{y}<extra>%{fullData.name}</extra>');
        if (Array.isArray(plotX) && Array.isArray(plotY)) {
            plotX.forEach((value, index) => {
                const yValue = plotY[index];
                if (typeof value === 'number') {
                    xMin = xMin == null ? value : Math.min(xMin, value);
                    xMax = xMax == null ? value : Math.max(xMax, value);
                }
                if (typeof yValue === 'number') {
                    yMin = yMin == null ? yValue : Math.min(yMin, yValue);
                    yMax = yMax == null ? yValue : Math.max(yMax, yValue);
                }
            });
        }
        const displayYear = getDisplayYear(year);
        traces.push({
            x: plotX,
            y: plotY,
            mode: 'lines',
            connectgaps: false,
            name: Number.isFinite(displayYear) ? displayYear.toString() : year.toString(),
            line: { color: colors[colorIdx % colors.length], width: 1.5 },
            opacity: 0.6,
            customdata,
            hovertemplate
        });
        colorIdx++;
    });

    // Current Year (2026)
    if (activeYears.includes(highlightYear) && data.series[highlightYear]) {
        const currentSeries = data.series[highlightYear];
        const dateLabels = buildDateLabels(currentSeries.x);
        let plotX = currentSeries.x;
        let plotY = currentSeries.y;
        let plotLabels = dateLabels;
        if (useRollingAxis) {
            const normalized = normalizeSeriesForRollingAxis(plotX, plotY, plotLabels);
            plotX = normalized.x;
            plotY = normalized.y;
            plotLabels = normalized.labels || plotLabels;
            plotX = shiftSeriesX(plotX, anchorDay, axisSpan);
            const clipped = clipSeriesToSpan(plotX, plotY, plotLabels, axisSpan);
            plotX = clipped.x;
            plotY = clipped.y;
            plotLabels = clipped.labels || plotLabels;
            const sorted = sortSeriesByX(plotX, plotY, plotLabels);
            plotX = TRADE_MATH.alignCycleStart(sorted.x, 1);
            plotY = sorted.y;
            plotLabels = sorted.customdata || plotLabels;
        }
        const continuous = keepLongestContinuousPlotSeries(plotX, plotY, plotLabels);
        plotX = continuous.x;
        plotY = continuous.y;
        plotLabels = continuous.labels || plotLabels;
        const contractLabel = getHoverContractLabel(highlightYear);
        const formulaLabel = buildContractFormulaForYear(highlightYear);
        const customdata = buildHoverCustomdata(plotLabels, contractLabel, formulaLabel);
        const hovertemplate = formulaLabel
            ? '<b>%{customdata[0]} · %{y}</b><br><b>%{customdata[1]}</b><br><b>%{customdata[2]}</b><extra><b>%{fullData.name}</b></extra>'
            : (contractLabel
                ? '<b>%{customdata[0]} · %{y}</b><br><b>%{customdata[1]}</b><extra><b>%{fullData.name}</b></extra>'
                : '<b>%{customdata[0]} · %{y}</b><extra><b>%{fullData.name}</b></extra>');
        if (Array.isArray(plotX) && Array.isArray(plotY)) {
            plotX.forEach((value, index) => {
                const yValue = plotY[index];
                if (typeof value === 'number') {
                    xMin = xMin == null ? value : Math.min(xMin, value);
                    xMax = xMax == null ? value : Math.max(xMax, value);
                }
                if (typeof yValue === 'number') {
                    yMin = yMin == null ? yValue : Math.min(yMin, yValue);
                    yMax = yMax == null ? yValue : Math.max(yMax, yValue);
                }
            });
        }
        traces.push({
            x: plotX,
            y: plotY,
            mode: 'lines',
            connectgaps: false,
            name: Number.isFinite(displayHighlightYear) ? displayHighlightYear.toString() : highlightYear.toString(),
            line: { color: '#EF4444', width: 2.5 }, // Red-500
            opacity: 1,
            customdata,
            hovertemplate
        });
    }

    const volumeSeries = null;

    const padding = yMin != null && yMax != null ? (yMax - yMin) * 0.08 : 0;
    const axisRange = yMin != null && yMax != null ? [yMin - padding, yMax + padding] : undefined;
    const axisConfig = useRollingAxis
        ? getRollingMonthAxisConfig(anchorDay, axisSpan, displayRange[0], displayRange[1])
        : getMonthAxisConfig(xMin, xMax);
    const rollingRange = displayRange;

    const layout = {
        autosize: true,
        paper_bgcolor: palette.plotPaper,
        plot_bgcolor: palette.plotBg,
        font: { family: 'Inter, Segoe UI, sans-serif', color: palette.plotFont, size: 10 },
        margin: { l: 54, r: 88, t: 24, b: 32 },
        showlegend: true,
        legend: {
            x: 1.02,
            y: 0.5,
            xanchor: 'left',
            yanchor: 'middle',
            bgcolor: palette.plotLegendBg,
            font: { color: palette.plotMuted, size: 9 }
        },
        xaxis: {
            showgrid: false,
            zerolinecolor: palette.plotZero,
            tickvals: axisConfig.tickvals,
            ticktext: axisConfig.ticktext,
            title: 'Month',
            titlefont: { color: palette.plotMuted, size: 9 },
            range: useRollingAxis ? rollingRange : (xMin != null && xMax != null ? [xMin, xMax] : undefined),
            linecolor: palette.plotLine,
            tickcolor: palette.plotLine,
            tickfont: { color: palette.plotMuted },
            ticks: 'outside',
            showspikes: false
        },
        yaxis: {
            gridcolor: palette.plotGrid,
            zerolinecolor: palette.plotZero,
            title: state.unit,
            titlefont: { color: palette.plotFont, size: 10 },
            linecolor: palette.plotLine,
            tickcolor: palette.plotLine,
            tickfont: { color: palette.plotMuted },
            ticks: 'outside',
            range: axisRange,
            autorange: axisRange ? false : true
        },
        hoverlabel: {
            bgcolor: palette.plotHoverBg,
            bordercolor: palette.plotHoverBorder,
            font: { color: palette.plotHoverFont }
        },
        hovermode: 'closest',
        shapes: buildMonthGridlines(axisConfig.gridlines)
    };

    if (volumeSeries) {
        layout.yaxis2 = {
            overlaying: 'y',
            side: 'right',
            showgrid: false,
            zeroline: false,
            showticklabels: false,
            ticks: ''
        };
    }

    const varOverlay = document.getElementById('var-overlay');
    if (varOverlay) {
        varOverlay.classList.remove('is-hidden');
    }


    Plotly.react('plotly-div', traces, layout, {displayModeBar: false, responsive: true});
    const plotContainer = document.getElementById('plotly-div');
    setContractAxisDiagnostics(
        plotContainer,
        traces,
        anchorDay,
        axisSpan,
        Number.isFinite(displayHighlightYear) ? displayHighlightYear : highlightYear
    );
    resizePlotOnNextFrame(plotContainer);

    lastVolatilityHistogram = state.showVar ? calculateVolatilityHistogram(data) : null;
    const histogram = document.getElementById('var-histogram');
    if (state.showVar && histogram && !histogram.classList.contains('is-hidden')) {
        renderVarHistogram(lastVolatilityHistogram);
    }

    // Update Overlay Stats
    const varStats = getVarStatsForData(data);
    const formula = getCleanChartTitle(displayHighlightYear);
    const overlayTitle = document.getElementById('overlay-title');
    if (overlayTitle) {
        overlayTitle.innerText = formula ? `${formula} (${state.unit})` : '';
    }
    const chartTitle = document.getElementById('chart-title');
    if (chartTitle) {
        chartTitle.innerText = formula;
    }
    const varYearLabel = document.getElementById('var-year-label');
    if (varYearLabel) {
        varYearLabel.innerText = '';
        varYearLabel.style.display = 'none';
    }
    document.getElementById('var-90').innerText = TRADE_MATH.format(varStats.p90);
    document.getElementById('var-95').innerText = TRADE_MATH.format(varStats.p95);
    document.getElementById('var-99').innerText = TRADE_MATH.format(varStats.p99);
}

function renderVarHistogram(histogram) {
    const container = document.getElementById('var-histogram-plot');
    if (!container) return;
    if (!histogram || !histogram.bins || !histogram.bins.length) {
        if (window.Plotly) {
            Plotly.purge(container);
        }
        container.innerHTML = '';
        return;
    }

    const bins = histogram.bins;
    const values = histogram.values;
    const palette = getThemePalette();
    const trace = {
        x: bins,
        y: values,
        type: 'bar',
        marker: { color: palette.plotVarBar },
        hovertemplate: '%{customdata}<extra></extra>',
        customdata: bins.map((center) => {
            const min = TRADE_MATH.format(center - 2.5, 1);
            const max = TRADE_MATH.format(center + 2.5, 1);
            return `${min}% to ${max}%`;
        })
    };

    const layout = {
        margin: { l: 36, r: 8, t: 6, b: 24 },
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: palette.plotBg,
        showlegend: false,
        xaxis: {
            tickfont: { color: palette.plotMuted, size: 8 },
            zeroline: true,
            zerolinecolor: palette.plotLine,
            gridcolor: palette.plotGrid,
            dtick: 5,
            title: '% change',
            titlefont: { color: palette.plotMuted, size: 8 }
        },
        yaxis: {
            tickfont: { color: palette.plotMuted, size: 8 },
            gridcolor: palette.plotGrid,
            ticksuffix: '%'
        }
    };

    Plotly.react('var-histogram-plot', [trace], layout, { displayModeBar: false, responsive: true });
    resizePlotOnNextFrame(container);
}

function renderVarSeasonalityChart(data) {
    const container = document.getElementById('var-seasonality-plot');
    if (!container) return;
    if (!state.showVar) {
        if (window.Plotly) {
            Plotly.purge(container);
        }
        container.innerHTML = '';
        lastVarSeasonality = null;
        return;
    }

    const seasonality = data ? calculateVarSeasonalitySeries(data) : null;
    if (!seasonality || !seasonality.series) {
        if (window.Plotly) {
            Plotly.purge(container);
        }
        container.innerHTML = '';
        lastVarSeasonality = null;
        return;
    }
    lastVarSeasonality = seasonality;

    const palette = getThemePalette();
    const traces = [];
    const colors = getSeriesColors();
    const sortedYears = Object.keys(seasonality.series).map(Number).sort((a, b) => a - b);
    const activeYears = resolveActiveYears(sortedYears);
    const highlightYear = activeYears.length ? Math.max(...activeYears) : getLatestYear();
    const displayHighlightYear = getDisplayYear(highlightYear);
    const axisSpan = ROLLING_AXIS_SPAN;
    const anchorDay = getContractAxisAnchorDay(data, activeYears);
    const useRollingAxis = Number.isFinite(anchorDay);
    const displayRange = [1, axisSpan];
    let colorIdx = 0;
    let xMin = null;
    let xMax = null;
    let yMin = null;
    let yMax = null;

    activeYears.forEach((year) => {
        if (year === highlightYear) return;
        const seriesData = seasonality.series[year];
        if (!seriesData) return;
        const dateLabels = buildDateLabels(seriesData.x);
        let plotX = seriesData.x;
        let plotY = seriesData.y;
        let plotLabels = dateLabels;
        if (useRollingAxis) {
            const normalized = normalizeSeriesForRollingAxis(plotX, plotY, plotLabels);
            plotX = normalized.x;
            plotY = normalized.y;
            plotLabels = normalized.labels || plotLabels;
            plotX = shiftSeriesX(plotX, anchorDay, axisSpan);
            const clipped = clipSeriesToSpan(plotX, plotY, plotLabels, axisSpan);
            plotX = clipped.x;
            plotY = clipped.y;
            plotLabels = clipped.labels || plotLabels;
            const sorted = sortSeriesByX(plotX, plotY, plotLabels);
            plotX = TRADE_MATH.alignCycleStart(sorted.x, 1);
            plotY = sorted.y;
            plotLabels = sorted.customdata || plotLabels;
        }
        const continuous = keepLongestContinuousPlotSeries(plotX, plotY, plotLabels);
        plotX = continuous.x;
        plotY = continuous.y;
        plotLabels = continuous.labels || plotLabels;
        const contractLabel = getHoverContractLabel(year);
        const formulaLabel = buildContractFormulaForYear(year);
        const customdata = buildHoverCustomdata(plotLabels, contractLabel, formulaLabel);
        const hovertemplate = formulaLabel
            ? '%{customdata[0]} · %{y}<br>%{customdata[1]}<br>%{customdata[2]}<extra>%{fullData.name}</extra>'
            : (contractLabel
                ? '%{customdata[0]} · %{y}<br>%{customdata[1]}<extra>%{fullData.name}</extra>'
                : '%{customdata[0]} · %{y}<extra>%{fullData.name}</extra>');
        plotX.forEach((value, index) => {
            const yValue = plotY[index];
            if (typeof value === 'number') {
                xMin = xMin == null ? value : Math.min(xMin, value);
                xMax = xMax == null ? value : Math.max(xMax, value);
            }
            if (typeof yValue === 'number') {
                yMin = yMin == null ? yValue : Math.min(yMin, yValue);
                yMax = yMax == null ? yValue : Math.max(yMax, yValue);
            }
        });
        const displayYear = getDisplayYear(year);
        traces.push({
            x: plotX,
            y: plotY,
            mode: 'lines',
            connectgaps: false,
            name: Number.isFinite(displayYear) ? displayYear.toString() : year.toString(),
            line: { color: colors[colorIdx % colors.length], width: 1.5 },
            opacity: 0.6,
            customdata,
            hovertemplate
        });
        colorIdx++;
    });

    if (activeYears.includes(highlightYear) && seasonality.series[highlightYear]) {
        const currentSeries = seasonality.series[highlightYear];
        const dateLabels = buildDateLabels(currentSeries.x);
        let plotX = currentSeries.x;
        let plotY = currentSeries.y;
        let plotLabels = dateLabels;
        if (useRollingAxis) {
            const normalized = normalizeSeriesForRollingAxis(plotX, plotY, plotLabels);
            plotX = normalized.x;
            plotY = normalized.y;
            plotLabels = normalized.labels || plotLabels;
            plotX = shiftSeriesX(plotX, anchorDay, axisSpan);
            const clipped = clipSeriesToSpan(plotX, plotY, plotLabels, axisSpan);
            plotX = clipped.x;
            plotY = clipped.y;
            plotLabels = clipped.labels || plotLabels;
            const sorted = sortSeriesByX(plotX, plotY, plotLabels);
            plotX = TRADE_MATH.alignCycleStart(sorted.x, 1);
            plotY = sorted.y;
            plotLabels = sorted.customdata || plotLabels;
        }
        const continuous = keepLongestContinuousPlotSeries(plotX, plotY, plotLabels);
        plotX = continuous.x;
        plotY = continuous.y;
        plotLabels = continuous.labels || plotLabels;
        const contractLabel = getHoverContractLabel(highlightYear);
        const formulaLabel = buildContractFormulaForYear(highlightYear);
        const customdata = buildHoverCustomdata(plotLabels, contractLabel, formulaLabel);
        const hovertemplate = formulaLabel
            ? '<b>%{customdata[0]} · %{y}</b><br><b>%{customdata[1]}</b><br><b>%{customdata[2]}</b><extra><b>%{fullData.name}</b></extra>'
            : (contractLabel
                ? '<b>%{customdata[0]} · %{y}</b><br><b>%{customdata[1]}</b><extra><b>%{fullData.name}</b></extra>'
                : '<b>%{customdata[0]} · %{y}</b><extra><b>%{fullData.name}</b></extra>');
        plotX.forEach((value, index) => {
            const yValue = plotY[index];
            if (typeof value === 'number') {
                xMin = xMin == null ? value : Math.min(xMin, value);
                xMax = xMax == null ? value : Math.max(xMax, value);
            }
            if (typeof yValue === 'number') {
                yMin = yMin == null ? yValue : Math.min(yMin, yValue);
                yMax = yMax == null ? yValue : Math.max(yMax, yValue);
            }
        });
        traces.push({
            x: plotX,
            y: plotY,
            mode: 'lines',
            connectgaps: false,
            name: Number.isFinite(displayHighlightYear) ? displayHighlightYear.toString() : highlightYear.toString(),
            line: { color: '#EF4444', width: 2.2 },
            opacity: 1,
            customdata,
            hovertemplate
        });
    }

    if (!traces.length) {
        if (window.Plotly) {
            Plotly.purge(container);
        }
        container.innerHTML = '';
        lastVarSeasonality = null;
        return;
    }

    const padding = yMin != null && yMax != null ? (yMax - yMin) * 0.08 : 0;
    const axisRange = yMin != null && yMax != null ? [yMin - padding, yMax + padding] : undefined;
    const axisConfig = useRollingAxis
        ? getRollingMonthAxisConfig(anchorDay, axisSpan, displayRange[0], displayRange[1])
        : getMonthAxisConfig(xMin, xMax);
    const rollingRange = displayRange;

    const layout = {
        autosize: true,
        margin: { l: 54, r: 88, t: 20, b: 32 },
        paper_bgcolor: 'rgba(0,0,0,0)',
        plot_bgcolor: palette.plotBg,
        font: { family: 'Inter, Segoe UI, sans-serif', color: palette.plotFont, size: 10 },
        showlegend: true,
        legend: {
            x: 1.02,
            y: 0.5,
            xanchor: 'left',
            yanchor: 'middle',
            bgcolor: palette.plotLegendBg,
            font: { color: palette.plotMuted, size: 9 }
        },
        xaxis: {
            showgrid: false,
            zerolinecolor: palette.plotZero,
            tickvals: axisConfig.tickvals,
            ticktext: axisConfig.ticktext,
            title: 'Month',
            titlefont: { color: palette.plotMuted, size: 9 },
            range: useRollingAxis ? rollingRange : (xMin != null && xMax != null ? [xMin, xMax] : undefined),
            linecolor: palette.plotLine,
            tickcolor: palette.plotLine,
            tickfont: { color: palette.plotMuted },
            ticks: 'outside',
            showspikes: false
        },
        yaxis: {
            gridcolor: palette.plotGrid,
            zerolinecolor: palette.plotZero,
            title: `VaR (90%) ${state.unit}`,
            titlefont: { color: palette.plotFont, size: 10 },
            linecolor: palette.plotLine,
            tickcolor: palette.plotLine,
            tickfont: { color: palette.plotMuted },
            ticks: 'outside',
            range: axisRange,
            autorange: axisRange ? false : true
        },
        hoverlabel: {
            bgcolor: palette.plotHoverBg,
            bordercolor: palette.plotHoverBorder,
            font: { color: palette.plotHoverFont }
        },
        hovermode: 'closest',
        shapes: buildMonthGridlines(axisConfig.gridlines)
    };

    Plotly.react('var-seasonality-plot', traces, layout, { displayModeBar: false, responsive: true });
    resizePlotOnNextFrame(container);
}

function refreshChartsForTheme() {
    if (!lastRenderedData) return;
    renderPlot(lastRenderedData);
    if (state.showVar && lastVolatilityHistogram) {
        renderVarHistogram(lastVolatilityHistogram);
    }
    renderVarSeasonalityChart(lastRenderedData);
}

window.addEventListener('themechange', refreshChartsForTheme);
