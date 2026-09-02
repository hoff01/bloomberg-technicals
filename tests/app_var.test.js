'use strict';

const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const TradeMath = require('../app/static/trade_math.js');

function loadAppFunctions() {
    const context = {
        window: {
            TradeMath,
            location: { protocol: 'http:' },
            addEventListener() {}
        },
        document: {
            addEventListener() {}
        },
        console,
        setTimeout() { return 0; },
        clearTimeout() {},
        setInterval() { return 0; },
        clearInterval() {},
        URL,
        URLSearchParams,
        TextDecoder,
        Uint8Array,
        Promise
    };
    vm.createContext(context);
    const source = fs.readFileSync(
        path.join(__dirname, '..', 'app', 'static', 'app.js'),
        'utf8'
    );
    vm.runInContext(source, context, { filename: 'app.js' });
    return context;
}

test('calculates VaR from full source history when the plotted cycle is partial', () => {
    const app = loadAppFunctions();
    const x = Array.from({ length: 80 }, (_, index) => 300 + index);
    const y = x.map((_, index) => 100 + (index * 0.1) + Math.sin(index / 3));

    const result = app.combineLegSeries([{
        leg: { code: 'HO', month: 'Dec', ratio: 1 },
        series: { 2026: { x, y } }
    }]);

    assert.equal(result.series[2026].y.length, 44);
    assert.equal(result.fullHistorySeries[2026].y.length, 80);
    assert.equal(app.calculateVarStats(result.series[2026].y).p90, 0);
    const stats = app.getVarStatsForData(result);
    assert.ok(stats.p90 > 0);
    assert.ok(stats.p95 > stats.p90);
    assert.ok(stats.p99 > stats.p95);

    const seasonality = app.calculateVarSeasonalitySeries(result);
    assert.equal(seasonality.series[2026].x.length, 44);
    assert.ok(seasonality.series[2026].y.some(Number.isFinite));
});

test('keeps missing PX_SETTLE values distinct from the default price series', () => {
    const app = loadAppFunctions();
    vm.runInContext("state.field = 'settle'", app);
    const commodity = {
        years: {
            2026: { x: [1, 2], y: [100, 101] }
        },
        fields: {
            PX_SETTLE: {
                2026: { x: [1, 2], y: [99, null] }
            }
        }
    };

    const selected = app.getFieldSeriesWithFallback(commodity, { 2026: [1, 2] });

    assert.deepEqual(selected[2026].y, [99, null]);
});