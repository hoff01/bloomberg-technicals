'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const TradeMath = require('../app/static/trade_math.js');

test('rounding is deterministic and capped at five decimals', () => {
    assert.equal(TradeMath.round5(1.23456789), 1.23457);
    assert.equal(TradeMath.round(-1.234565, 5), -1.23457);
    assert.equal(TradeMath.round(12.3456789, 9), 12.34568);
    assert.equal(TradeMath.round5(-0.0000001), 0);
    assert.equal(TradeMath.round5(Number.NaN), null);
});

test('converts cpg, dollars per gallon, dollars per barrel, and dollars per MT', () => {
    const factors = { bbl_per_mt: 7.33, gal_per_bbl: 42 };
    assert.equal(TradeMath.convertValue(100, 'cpg', '$/gal', factors), 1);
    assert.equal(TradeMath.convertValue(100, 'cpg', '$/bbl', factors), 42);
    assert.equal(TradeMath.convertValue(42, '$/bbl', 'cpg', factors), 100);
    assert.equal(TradeMath.convertValue(733, '$/MT', '$/bbl', factors), 100);
    assert.equal(TradeMath.convertValue(1, '$/gal', '$/MT', factors), 307.86);
});

test('converts every mixed-unit leg before applying weights', () => {
    const result = TradeMath.weightedSum([
        { value: 100, native_unit: 'cpg', ratio: 1 },
        { value: 42, native_unit: '$/bbl', ratio: -0.5 },
        { value: 7.33, native_unit: '$/MT', ratio: 1 }
    ], '$/bbl', { bbl_per_mt: 7.33, gal_per_bbl: 42 });
    assert.equal(result, 22);
});

test('interpolates a holiday mismatch only between two nearby observations', () => {
    const aligned = TradeMath.interpolateSeriesAtTargets(
        { x: [1, 3], y: [10, 14] },
        [0, 1, 2, 3, 4]
    );
    assert.deepEqual(aligned, {
        x: [0, 1, 2, 3, 4],
        y: [null, 10, 12, 14, null],
        interpolated: [false, false, true, false, false]
    });
});

test('fills every bounded interior day so a contract line remains continuous', () => {
    const continuous = TradeMath.interpolateInteriorSeries(
        { x: [1, 3, 10], y: [10, 14, 30] }
    );

    assert.deepEqual(continuous, {
        x: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        y: [10, 12, 14, 16.28571, 18.57143, 20.85714, 23.14286, 25.42857, 27.71429, 30],
        interpolated: [false, true, false, true, true, true, true, true, true, false],
        interpolatedPoints: 7
    });
});

test('continuous interpolation does not extrapolate outside known endpoints', () => {
    const continuous = TradeMath.interpolateInteriorSeries(
        { x: [3, 5], y: [12, 16] }
    );

    assert.deepEqual(continuous, {
        x: [3, 4, 5],
        y: [12, 14, 16],
        interpolated: [false, true, false],
        interpolatedPoints: 1
    });
});

test('bounded interpolation preserves long missing intervals as nulls', () => {
    const continuous = TradeMath.interpolateInteriorSeries(
        { x: [1, 3, 30], y: [10, 14, 40] },
        TradeMath.DEFAULT_INTERPOLATION_MAX_SPAN_DAYS
    );

    assert.equal(continuous.y[1], 12);
    assert.equal(continuous.y[3], null);
    assert.equal(continuous.y[28], null);
    assert.equal(continuous.y[29], 40);
});

test('keeps the longest continuous chart run instead of exposing a gap', () => {
    assert.deepEqual(
        TradeMath.longestContinuousSeries(
            { x: [140, 141, 203, 204], y: [80, 81, 70, 71] }
        ),
        {
            x: [140, 141],
            y: [80, 81],
            sourceIndexes: [0, 1]
        }
    );
});

test('rebases actual contract years to formula base years per leg', () => {
    const janPlusOne = {
        2026: { x: [1], y: [60] },
        2027: { x: [1], y: [70] }
    };

    assert.deepEqual(TradeMath.rebaseSeriesYears(janPlusOne, 1), {
        2025: { x: [1], y: [60] },
        2026: { x: [1], y: [70] }
    });
    assert.deepEqual(TradeMath.rebaseSeriesYears(janPlusOne, 0), janPlusOne);
});

test('builds mixed-offset contracts from one selected formula year', () => {
    const config = {
        yellow_key: 'Comdty',
        ticker_template: '{root}{month_code}{yy} {yellow_key}'
    };
    const baseYear = 2026;

    assert.equal(
        TradeMath.buildTicker('HO', 'Dec', TradeMath.contractYearForOffset(baseYear, 0), config),
        'HOZ26 Comdty'
    );
    assert.equal(
        TradeMath.buildTicker('HO', 'Jan', TradeMath.contractYearForOffset(baseYear, 1), config),
        'HOF27 Comdty'
    );
});

test('fills the non-leap February 29 placeholder instead of breaking the line', () => {
    const continuous = TradeMath.interpolateInteriorSeries(
        { x: [59, 60, 61], y: [100, null, 104] }
    );

    assert.deepEqual(continuous, {
        x: [59, 60, 61],
        y: [100, 102, 104],
        interpolated: [false, true, false],
        interpolatedPoints: 1
    });
});

test('normalizes the leap template to an exact 365-day contract axis', () => {
    assert.equal(TradeMath.sourceCycleDay(518), 152);
    assert.equal(TradeMath.toContractCycleDay(59), 59);
    assert.equal(TradeMath.toContractCycleDay(60), null);
    assert.equal(TradeMath.toContractCycleDay(61), 60);
    assert.equal(TradeMath.toContractCycleDay(366), 365);
    assert.equal(TradeMath.toContractCycleDay(367), 366);
    assert.equal(TradeMath.toContractCycleDay(518), 516);

    assert.deepEqual(
        TradeMath.normalizeContractSeries({
            x: [59, 60, 61, 366, 367, 518],
            y: [1, 2, 3, 4, 5, 6]
        }),
        {
            x: [59, 60, 365, 366, 516],
            y: [1, 3, 4, 5, 6],
            sourceIndexes: [0, 2, 3, 4, 5]
        }
    );
});

test('uses the latest selected-leg endpoint as the shared cycle end', () => {
    const latestEnd = TradeMath.latestContractEndDay([
        { x: [153, 509], y: [10, 11] },
        { x: [153, 511], y: [20, 21] }
    ]);

    assert.equal(latestEnd, 144);
    assert.equal(TradeMath.rotateCycleDay(145, latestEnd, 365), 1);
    assert.equal(TradeMath.rotateCycleDay(latestEnd, latestEnd, 365), 365);
});

test('puts a partial current contract at the right edge instead of mid-chart', () => {
    const latestEnd = TradeMath.latestContractEndDay([
        { x: [153, 218], y: [10, 11] }
    ]);

    assert.equal(latestEnd, 217);
    assert.equal(TradeMath.rotateCycleDay(218, latestEnd, 365), 1);
    assert.equal(TradeMath.rotateCycleDay(217, latestEnd, 365), 365);
});

test('aligns partial and complete traces at the shared season start', () => {
    assert.deepEqual(TradeMath.alignCycleStart([203, 204, 365], 1), [1, 2, 163]);
    assert.deepEqual(TradeMath.alignCycleStart([1, 2, 365], 1), [1, 2, 365]);
    assert.deepEqual(TradeMath.alignCycleStart([], 1), []);
});

test('combines holiday-mismatched legs without filling long or unbounded gaps', () => {
    const combined = TradeMath.combineWeightedSeries([
        { series: { x: [1, 3, 10], y: [10, 14, 30] }, ratio: 1, native_unit: '$/bbl' },
        { series: { x: [1, 2, 3, 5, 10], y: [3, 4, 5, 6, 7] }, ratio: -1, native_unit: '$/bbl' }
    ], '$/bbl', { maxSpanDays: 4 });

    assert.deepEqual(combined, {
        x: [1, 2, 3, 10],
        y: [7, 8, 9, 23],
        interpolatedPoints: 1
    });
});

test('builds Bloomberg-style WU and HO tickers with yellow keys', () => {
    const config = {
        yellow_key: 'Comdty',
        ticker_template: '{root}{month_code}{year_2d} {yellow_key}'
    };
    assert.equal(TradeMath.buildTicker('WU', 'Jan', 2026, config), 'WUF26 Comdty');
    assert.equal(TradeMath.buildTicker('HO', 'F', 2026, config), 'HOF26 Comdty');
    assert.equal(TradeMath.buildTicker('HO', 'Feb', 2026, {
        yellow_key: 'Comdty',
        ticker_template: '{root}{month_code}{y} {yellow_key}'
    }), 'HOG26 Comdty');
    assert.equal(TradeMath.buildTicker('WU', 'Sep', 2026, {
        yellow_key: 'Index',
        ticker_template: '{root}{month_code}{year_1d} {yellow_key}'
    }), 'WUU26 Index');
});

test('parses WUF26 and HOF26 contracts instead of matching a literal backslash-d', () => {
    assert.deepEqual(TradeMath.parseTicker('WUF26'), {
        raw: 'WUF26', root: 'WU', monthCode: 'F', month: 'Jan', year: 2026, yearDigits: '26', yellowKey: ''
    });
    assert.deepEqual(TradeMath.parseTicker('HOF26 Comdty'), {
        raw: 'HOF26 Comdty', root: 'HO', monthCode: 'F', month: 'Jan', year: 2026, yearDigits: '26', yellowKey: 'Comdty'
    });
});

test('honors a custom ticker template', () => {
    const ticker = TradeMath.buildTicker('WU', 'Dec', 2027, {
        yellow_key: 'Index',
        ticker_template: '{root}-{month_code}-{yyyy} {yellow_key}'
    });
    assert.equal(ticker, 'WU-Z-2027 Index');
});

test('normalizes RVO as a flat monthless curve', () => {
    assert.equal(TradeMath.normalizeCurveMode('Flat'), 'flat');
    assert.equal(TradeMath.normalizeCurveMode('flat forward'), 'flat');
    assert.equal(TradeMath.normalizeCurveMode('Monthly'), 'monthly');
    assert.equal(TradeMath.normalizeCurveMode(''), 'monthly');
});

test('preserves monthly roll months and fixed contract-cycle boundaries', () => {
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    assert.deepEqual(months.map((month) => TradeMath.rollMonthForPeriod(month)), months);
    assert.equal(TradeMath.rollMonthForPeriod('Q2'), 'Apr');
    assert.equal(TradeMath.rollMonthForPeriod('Q3'), 'Jul');
    assert.equal(TradeMath.rollMonthForPeriod('Q4'), 'Oct');
    assert.equal(TradeMath.rollMonthForPeriod('Half 2'), 'Jul');
    assert.equal(TradeMath.contractCycleAnchorDay(1), 365);
    assert.equal(TradeMath.contractCycleAnchorDay(6), 151);
    assert.equal(TradeMath.contractCycleAnchorDay(8), 212);
    assert.equal(TradeMath.contractCycleAnchorDay(13), null);
});

test('stitches adjacent references into one physical contract cycle', () => {
    const december = TradeMath.mergeReferenceSeries([
        {
            reference: 2,
            series: { 2026: { x: [519, 701], y: [80, 90] } }
        },
        {
            reference: 1,
            series: { 2026: { x: [336, 518], y: [91, 100] } }
        }
    ], 1);

    assert.deepEqual(december, {
        2026: {
            x: [153, 335, 336, 518],
            y: [80, 90, 91, 100]
        }
    });
    assert.deepEqual(
        TradeMath.clipSeriesToSourceCycle(december[2026], 6),
        december[2026]
    );
    assert.deepEqual(TradeMath.sourceCycleWindow(6), { start: 153, end: 518 });
});

test('aligns each daily flat value to the selected forward-curve cycle', () => {
    const aligned = TradeMath.alignFlatCurveSeries({
        2025: { x: [1, 31, 32, 366], y: [10, 11, 12, 13] },
        2026: { x: [1, 31, 32], y: [20, 21, 22] }
    }, [2026], 2);

    assert.deepEqual(aligned, {
        2026: { x: [32, 366, 367, 397], y: [12, 13, 20, 21] }
    });
    assert.deepEqual(
        TradeMath.alignFlatCurveSeries({ 2025: { x: [1, 366], y: [10, 13] } }, [2026], 1),
        { 2026: { x: [1, 366], y: [10, 13] } }
    );
});
