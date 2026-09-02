(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) {
        module.exports = api;
    }
    root.TradeMath = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    const MAX_DECIMALS = 5;
    const DEFAULT_INTERPOLATION_MAX_SPAN_DAYS = 7;
    const DEFAULT_BBL_PER_MT = 7.33;
    const DEFAULT_GAL_PER_BBL = 42;
    const SOURCE_AXIS_SPAN = 366;
    const CONTRACT_AXIS_SPAN = 365;
    const CONTRACT_MONTH_AXIS_BOUNDARIES = Object.freeze([1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335, 366]);
    const MONTH_CODES = Object.freeze({
        JAN: 'F', FEB: 'G', MAR: 'H', APR: 'J', MAY: 'K', JUN: 'M',
        JUL: 'N', AUG: 'Q', SEP: 'U', OCT: 'V', NOV: 'X', DEC: 'Z'
    });
    const MONTH_NAMES = Object.freeze(Object.entries(MONTH_CODES).reduce((acc, entry) => {
        acc[entry[1]] = entry[0][0] + entry[0].slice(1).toLowerCase();
        return acc;
    }, {}));
    const CONTRACT_MONTH_PATTERN = '[FGHJKMNQUVXZ]';
    const MONTH_AXIS_BOUNDARIES = Object.freeze([1, 32, 61, 92, 122, 153, 183, 214, 245, 275, 306, 336, 367]);

    function asFinite(value) {
        if (value === '' || value == null) return null;
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    }

    function sourceCycleDay(value) {
        const sourceDay = asFinite(value);
        if (sourceDay == null) return null;
        return ((sourceDay - 1) % SOURCE_AXIS_SPAN + SOURCE_AXIS_SPAN) % SOURCE_AXIS_SPAN + 1;
    }

    function toContractCycleDay(value) {
        const sourceDay = asFinite(value);
        if (sourceDay == null) return null;
        const cycleIndex = Math.floor((sourceDay - 1) / SOURCE_AXIS_SPAN);
        const leapTemplateDay = sourceCycleDay(sourceDay);
        if (leapTemplateDay === 60) return null;
        const nonLeapDay = leapTemplateDay > 60 ? leapTemplateDay - 1 : leapTemplateDay;
        return (cycleIndex * CONTRACT_AXIS_SPAN) + nonLeapDay;
    }

    function normalizeContractSeries(series) {
        if (!series || !Array.isArray(series.x) || !Array.isArray(series.y)) {
            return { x: [], y: [], sourceIndexes: [] };
        }
        const x = [];
        const y = [];
        const sourceIndexes = [];
        const length = Math.min(series.x.length, series.y.length);
        for (let index = 0; index < length; index++) {
            const contractDay = toContractCycleDay(series.x[index]);
            if (contractDay == null) continue;
            x.push(contractDay);
            y.push(series.y[index]);
            sourceIndexes.push(index);
        }
        return { x, y, sourceIndexes };
    }

    function latestContractEndDay(seriesList) {
        const candidates = Array.isArray(seriesList) ? seriesList : [];
        let latestExtendedDay = null;
        candidates.forEach((series) => {
            if (!series || !Array.isArray(series.x) || !Array.isArray(series.y)) return;
            const length = Math.min(series.x.length, series.y.length);
            for (let index = 0; index < length; index++) {
                if (asFinite(series.y[index]) == null) continue;
                const contractDay = toContractCycleDay(series.x[index]);
                if (contractDay == null) continue;
                latestExtendedDay = latestExtendedDay == null
                    ? contractDay
                    : Math.max(latestExtendedDay, contractDay);
            }
        });
        if (latestExtendedDay == null) return null;
        return ((latestExtendedDay - 1) % CONTRACT_AXIS_SPAN + CONTRACT_AXIS_SPAN) % CONTRACT_AXIS_SPAN + 1;
    }

    function rotateCycleDay(value, anchorDay, span) {
        const day = asFinite(value);
        const anchor = asFinite(anchorDay);
        const requestedSpan = asFinite(span);
        const cycleSpan = requestedSpan != null && requestedSpan > 0
            ? requestedSpan
            : CONTRACT_AXIS_SPAN;
        if (day == null || anchor == null) return value;
        const normalized = ((day - 1) % cycleSpan + cycleSpan) % cycleSpan + 1;
        const offset = normalized - anchor;
        return offset <= 0 ? offset + cycleSpan : offset;
    }

    function contractCycleAnchorDay(rollMonthIndex) {
        const monthIndex = Math.trunc(Number(rollMonthIndex));
        if (monthIndex < 1 || monthIndex > 12) return null;
        const boundary = CONTRACT_MONTH_AXIS_BOUNDARIES[monthIndex - 1];
        return boundary === 1 ? CONTRACT_AXIS_SPAN : boundary - 1;
    }

    function sourceCycleWindow(rollMonthIndex) {
        const monthIndex = Math.trunc(Number(rollMonthIndex));
        if (monthIndex < 1 || monthIndex > 12) return null;
        const start = MONTH_AXIS_BOUNDARIES[monthIndex - 1];
        return { start, end: start + SOURCE_AXIS_SPAN - 1 };
    }

    function alignCycleStart(values, targetStart) {
        if (!Array.isArray(values)) return [];
        const first = values.find((value) => asFinite(value) != null);
        const start = asFinite(targetStart);
        if (first == null || start == null) return values.slice();
        const offset = first - start;
        return values.map((value) => {
            const day = asFinite(value);
            return day == null ? value : day - offset;
        });
    }

    function round(value, decimals) {
        const number = asFinite(value);
        if (number == null) return null;
        const requested = decimals == null ? MAX_DECIMALS : Math.trunc(Number(decimals));
        const places = Number.isFinite(requested)
            ? Math.max(0, Math.min(MAX_DECIMALS, requested))
            : MAX_DECIMALS;
        const factor = 10 ** places;
        const sign = number < 0 ? -1 : 1;
        const result = sign * (Math.round((Math.abs(number) + Number.EPSILON) * factor) / factor);
        return Object.is(result, -0) ? 0 : result;
    }

    function round5(value) {
        return round(value, MAX_DECIMALS);
    }

    function format(value, decimals) {
        const rounded = round(value, decimals);
        return rounded == null ? '' : String(rounded);
    }

    function normalizeUnit(unit) {
        const value = String(unit == null ? '' : unit)
            .trim()
            .replace(/\s+/g, '')
            .toLowerCase();
        if (value === 'cpg' || value === 'cents/gal' || value === 'cents/gallon') return 'cpg';
        if (value === '$/gal' || value === 'usd/gal' || value === '$/gallon' || value === 'usd/gallon') return '$/gal';
        if (value === '$/bbl' || value === 'usd/bbl' || value === '$/barrel' || value === 'usd/barrel') return '$/bbl';
        if (value === '$/mt' || value === 'usd/mt' || value === '$/metricton' || value === 'usd/metricton') return '$/MT';
        return '';
    }

    function normalizeCurveMode(value) {
        const mode = String(value == null ? '' : value)
            .trim()
            .replace(/[\s_-]+/g, '')
            .toLowerCase();
        if (mode === 'flat' || mode === 'flatforward' || mode === 'monthless' || mode === 'spot') {
            return 'flat';
        }
        return 'monthly';
    }

    function rollMonthForPeriod(value) {
        const label = String(value == null ? '' : value).trim();
        if (!label) return 'Jan';
        const upper = label.toUpperCase().replace(/[-_]+/g, ' ').replace(/\s+/g, ' ');
        if (upper === 'Q2' || upper === 'QUARTER 2') return 'Apr';
        if (upper === 'Q3' || upper === 'QUARTER 3') return 'Jul';
        if (upper === 'Q4' || upper === 'QUARTER 4') return 'Oct';
        if (upper === 'HALF 2' || upper === 'H2' || upper === '2H') return 'Jul';
        if (
            upper === 'Q1'
            || upper === 'QUARTER 1'
            || upper === 'HALF 1'
            || upper === 'H1'
            || upper === '1H'
        ) {
            return 'Jan';
        }
        const month = Object.keys(MONTH_CODES).find((name) => name === upper.slice(0, 3));
        return month ? month[0] + month.slice(1).toLowerCase() : 'Jan';
    }

    function alignFlatCurveSeries(seriesMap, targetYears, rollMonthIndex) {
        const source = seriesMap && typeof seriesMap === 'object' ? seriesMap : {};
        const monthIndex = Math.trunc(Number(rollMonthIndex));
        if (monthIndex < 1 || monthIndex > 12 || !Array.isArray(targetYears)) return {};
        const boundary = MONTH_AXIS_BOUNDARIES[monthIndex - 1];
        const aligned = {};

        function appendWindow(output, series, predicate, offset) {
            if (!series || !Array.isArray(series.x) || !Array.isArray(series.y)) return;
            const length = Math.min(series.x.length, series.y.length);
            for (let index = 0; index < length; index++) {
                const x = Number(series.x[index]);
                if (!Number.isFinite(x) || !predicate(x)) continue;
                output.push({ x: x + offset, y: series.y[index] });
            }
        }

        targetYears.forEach((targetYearValue) => {
            const targetYear = Number(targetYearValue);
            if (!Number.isFinite(targetYear)) return;
            const points = [];
            appendWindow(points, source[String(targetYear - 1)] || source[targetYear - 1], (x) => x >= boundary, 0);
            if (monthIndex > 1) {
                appendWindow(points, source[String(targetYear)] || source[targetYear], (x) => x < boundary, 366);
            }
            if (!points.length) return;
            points.sort((a, b) => a.x - b.x);
            aligned[String(targetYear)] = {
                x: points.map((point) => point.x),
                y: points.map((point) => point.y)
            };
        });
        return aligned;
    }

    function normalizeConversionConfig(config) {
        const source = config && typeof config === 'object' ? config : {};
        const bblPerMT = asFinite(source.bblPerMT != null ? source.bblPerMT : source.bbl_per_mt);
        const galPerBbl = asFinite(source.galPerBbl != null ? source.galPerBbl : source.gal_per_bbl);
        return {
            bblPerMT: bblPerMT != null && bblPerMT > 0 ? bblPerMT : DEFAULT_BBL_PER_MT,
            galPerBbl: galPerBbl != null && galPerBbl > 0 ? galPerBbl : DEFAULT_GAL_PER_BBL
        };
    }

    function toDollarsPerGallon(value, fromUnit, config) {
        const unit = normalizeUnit(fromUnit);
        const factors = normalizeConversionConfig(config);
        if (unit === 'cpg') return value / 100;
        if (unit === '$/gal') return value;
        if (unit === '$/bbl') return value / factors.galPerBbl;
        if (unit === '$/MT') return value / (factors.bblPerMT * factors.galPerBbl);
        return null;
    }

    function fromDollarsPerGallon(value, targetUnit, config) {
        const unit = normalizeUnit(targetUnit);
        const factors = normalizeConversionConfig(config);
        if (unit === 'cpg') return value * 100;
        if (unit === '$/gal') return value;
        if (unit === '$/bbl') return value * factors.galPerBbl;
        if (unit === '$/MT') return value * factors.galPerBbl * factors.bblPerMT;
        return null;
    }

    function convertValue(value, fromUnit, targetUnit, config) {
        const number = asFinite(value);
        const from = normalizeUnit(fromUnit);
        const target = normalizeUnit(targetUnit);
        if (number == null || !from || !target) return null;
        if (from === target) return round5(number);
        const dollarsPerGallon = toDollarsPerGallon(number, from, config);
        if (dollarsPerGallon == null) return null;
        return round5(fromDollarsPerGallon(dollarsPerGallon, target, config));
    }

    function conversionFactor(fromUnit, targetUnit, config) {
        return convertValue(1, fromUnit, targetUnit, config);
    }

    function weightedSum(legs, targetUnit, defaultConfig) {
        if (!Array.isArray(legs) || !legs.length) return null;
        let total = 0;
        let used = 0;
        for (const leg of legs) {
            if (!leg) return null;
            const ratio = asFinite(leg.ratio != null ? leg.ratio : leg.weight);
            const value = asFinite(leg.value);
            if (ratio == null || value == null) return null;
            if (ratio === 0) continue;
            const converted = convertValue(
                value,
                leg.nativeUnit || leg.native_unit || leg.unit || targetUnit,
                targetUnit,
                leg.config || defaultConfig
            );
            if (converted == null) return null;
            total += converted * ratio;
            used += 1;
        }
        return used ? round5(total) : 0;
    }

    function interpolateSeriesAtTargets(series, targets, maxSpanDays) {
        const outputX = Array.isArray(targets) ? targets.map(Number) : [];
        const outputY = Array(outputX.length).fill(null);
        const interpolated = Array(outputX.length).fill(false);
        if (!series || !Array.isArray(series.x) || !Array.isArray(series.y) || !outputX.length) {
            return { x: outputX, y: outputY, interpolated };
        }

        const byX = new Map();
        const length = Math.min(series.x.length, series.y.length);
        for (let index = 0; index < length; index++) {
            const x = asFinite(series.x[index]);
            const y = asFinite(series.y[index]);
            if (x == null || y == null) continue;
            byX.set(x, y);
        }
        const points = Array.from(byX, ([x, y]) => ({ x, y })).sort((a, b) => a.x - b.x);
        if (!points.length) return { x: outputX, y: outputY, interpolated };

        const requestedSpan = asFinite(maxSpanDays);
        const maxSpan = maxSpanDays === Number.POSITIVE_INFINITY
            ? Number.POSITIVE_INFINITY
            : (requestedSpan != null && requestedSpan > 0
                ? requestedSpan
                : DEFAULT_INTERPOLATION_MAX_SPAN_DAYS);

        outputX.forEach((target, outputIndex) => {
            if (!Number.isFinite(target)) return;
            if (byX.has(target)) {
                outputY[outputIndex] = byX.get(target);
                return;
            }

            let low = 0;
            let high = points.length;
            while (low < high) {
                const middle = Math.floor((low + high) / 2);
                if (points[middle].x < target) low = middle + 1;
                else high = middle;
            }
            const previous = low > 0 ? points[low - 1] : null;
            const next = low < points.length ? points[low] : null;
            if (!previous || !next || previous.x >= target || next.x <= target) return;
            const span = next.x - previous.x;
            if (!Number.isFinite(span) || span <= 0 || span > maxSpan) return;
            const fraction = (target - previous.x) / span;
            outputY[outputIndex] = round5(previous.y + ((next.y - previous.y) * fraction));
            interpolated[outputIndex] = true;
        });

        return { x: outputX, y: outputY, interpolated };
    }

    function interpolateInteriorSeries(series, maxSpanDays) {
        if (!series || !Array.isArray(series.x) || !Array.isArray(series.y)) {
            return { x: [], y: [], interpolated: [], interpolatedPoints: 0 };
        }

        const targetSet = new Set();
        const length = Math.min(series.x.length, series.y.length);
        let minimum = null;
        let maximum = null;
        for (let index = 0; index < length; index++) {
            const x = asFinite(series.x[index]);
            const y = asFinite(series.y[index]);
            if (x == null || y == null) continue;
            targetSet.add(x);
            minimum = minimum == null ? x : Math.min(minimum, x);
            maximum = maximum == null ? x : Math.max(maximum, x);
        }
        if (minimum == null || maximum == null) {
            return { x: [], y: [], interpolated: [], interpolatedPoints: 0 };
        }

        for (let target = Math.ceil(minimum); target <= Math.floor(maximum); target++) {
            targetSet.add(target);
        }
        const targets = Array.from(targetSet).sort((a, b) => a - b);
        const aligned = interpolateSeriesAtTargets(
            series,
            targets,
            maxSpanDays == null ? Number.POSITIVE_INFINITY : maxSpanDays
        );
        return {
            ...aligned,
            interpolatedPoints: aligned.interpolated.filter(Boolean).length
        };
    }

    function longestContinuousSeries(series, maxSpanDays) {
        if (!series || !Array.isArray(series.x) || !Array.isArray(series.y)) {
            return { x: [], y: [], sourceIndexes: [] };
        }
        const requestedSpan = asFinite(maxSpanDays);
        const maxSpan = requestedSpan != null && requestedSpan > 0
            ? requestedSpan
            : DEFAULT_INTERPOLATION_MAX_SPAN_DAYS;
        const length = Math.min(series.x.length, series.y.length);
        let bestStart = 0;
        let bestLength = 0;
        let runStart = 0;
        let runLength = 0;
        let previousX = null;

        for (let index = 0; index < length; index++) {
            const currentX = asFinite(series.x[index]);
            const currentY = asFinite(series.y[index]);
            const continues = currentX != null
                && currentY != null
                && (previousX == null || currentX - previousX <= maxSpan);
            if (!continues) {
                runStart = index;
                runLength = currentX != null && currentY != null ? 1 : 0;
            } else {
                runLength += 1;
            }
            if (runLength > bestLength) {
                bestStart = runStart;
                bestLength = runLength;
            }
            previousX = currentX != null && currentY != null ? currentX : null;
        }

        const sourceIndexes = Array.from({ length: bestLength }, (_, index) => bestStart + index);
        return {
            x: sourceIndexes.map((index) => series.x[index]),
            y: sourceIndexes.map((index) => series.y[index]),
            sourceIndexes
        };
    }

    function rebaseSeriesYears(seriesMap, yearOffset) {
        if (!seriesMap || typeof seriesMap !== 'object') return {};
        const parsedOffset = asFinite(yearOffset);
        const offset = parsedOffset == null ? 0 : Math.trunc(parsedOffset);
        const rebased = {};
        Object.entries(seriesMap).forEach(([year, series]) => {
            const actualYear = asFinite(year);
            if (actualYear == null) return;
            rebased[String(Math.trunc(actualYear) - offset)] = series;
        });
        return rebased;
    }

    function mergeReferenceSeries(segments, targetReference) {
        const parsedTarget = asFinite(targetReference);
        if (!Array.isArray(segments) || parsedTarget == null) return {};
        const target = Math.trunc(parsedTarget);
        const pointsByYear = new Map();

        segments.forEach((segment) => {
            const reference = asFinite(segment && segment.reference);
            const seriesMap = segment && segment.series;
            if (reference == null || !seriesMap || typeof seriesMap !== 'object') return;
            const shift = (target - Math.trunc(reference)) * SOURCE_AXIS_SPAN;
            Object.entries(seriesMap).forEach(([year, series]) => {
                if (!series || !Array.isArray(series.x) || !Array.isArray(series.y)) return;
                let points = pointsByYear.get(String(year));
                if (!points) {
                    points = new Map();
                    pointsByYear.set(String(year), points);
                }
                const length = Math.min(series.x.length, series.y.length);
                for (let index = 0; index < length; index++) {
                    const x = asFinite(series.x[index]);
                    const y = asFinite(series.y[index]);
                    if (x == null || y == null) continue;
                    points.set(x + shift, y);
                }
            });
        });

        const merged = {};
        pointsByYear.forEach((points, year) => {
            const entries = Array.from(points.entries()).sort((left, right) => left[0] - right[0]);
            if (!entries.length) return;
            merged[year] = {
                x: entries.map((entry) => entry[0]),
                y: entries.map((entry) => entry[1])
            };
        });
        return merged;
    }

    function clipSeriesToSourceCycle(series, rollMonthIndex) {
        const window = sourceCycleWindow(rollMonthIndex);
        if (!window || !series || !Array.isArray(series.x) || !Array.isArray(series.y)) {
            return { x: [], y: [] };
        }
        const x = [];
        const y = [];
        const length = Math.min(series.x.length, series.y.length);
        for (let index = 0; index < length; index++) {
            const value = asFinite(series.x[index]);
            if (value == null || value < window.start || value > window.end) continue;
            x.push(value);
            y.push(series.y[index]);
        }
        return { x, y };
    }

    function contractYearForOffset(baseYear, yearOffset) {
        const parsedYear = asFinite(baseYear);
        if (parsedYear == null) return null;
        const parsedOffset = asFinite(yearOffset);
        const offset = parsedOffset == null ? 0 : Math.trunc(parsedOffset);
        return Math.trunc(parsedYear) + offset;
    }

    function combineWeightedSeries(legs, targetUnit, options) {
        if (!Array.isArray(legs) || !legs.length) {
            return { x: [], y: [], interpolatedPoints: 0 };
        }
        const settings = options && typeof options === 'object' ? options : {};
        const maxSpanDays = settings.maxSpanDays;
        const prepared = [];
        const targetSet = new Set();

        for (const leg of legs) {
            if (!leg || !leg.series || !Array.isArray(leg.series.x) || !Array.isArray(leg.series.y)) {
                return { x: [], y: [], interpolatedPoints: 0 };
            }
            const ratio = asFinite(leg.ratio != null ? leg.ratio : leg.weight);
            if (ratio == null) return { x: [], y: [], interpolatedPoints: 0 };
            if (ratio === 0) continue;
            const length = Math.min(leg.series.x.length, leg.series.y.length);
            for (let index = 0; index < length; index++) {
                const x = asFinite(leg.series.x[index]);
                const y = asFinite(leg.series.y[index]);
                if (x != null && y != null) targetSet.add(x);
            }
            prepared.push({ ...leg, ratio });
        }
        if (!prepared.length) {
            return { x: [], y: [], interpolatedPoints: 0 };
        }

        const targets = Array.from(targetSet).sort((a, b) => a - b);
        const aligned = prepared.map((leg) => (
            interpolateSeriesAtTargets(leg.series, targets, maxSpanDays)
        ));
        const x = [];
        const y = [];
        let interpolatedPoints = 0;

        targets.forEach((target, targetIndex) => {
            const weightedLegs = [];
            let usedInterpolation = 0;
            for (let legIndex = 0; legIndex < prepared.length; legIndex++) {
                const value = aligned[legIndex].y[targetIndex];
                if (!Number.isFinite(value)) return;
                if (aligned[legIndex].interpolated[targetIndex]) usedInterpolation += 1;
                const leg = prepared[legIndex];
                weightedLegs.push({
                    value,
                    ratio: leg.ratio,
                    native_unit: leg.nativeUnit || leg.native_unit || leg.unit || targetUnit,
                    config: leg.config
                });
            }
            const total = weightedSum(weightedLegs, targetUnit, settings.defaultConfig);
            if (!Number.isFinite(total)) return;
            x.push(target);
            y.push(total);
            interpolatedPoints += usedInterpolation;
        });

        return { x, y, interpolatedPoints };
    }

    function monthToCode(month) {
        const value = String(month == null ? '' : month).trim().toUpperCase();
        if (new RegExp(`^${CONTRACT_MONTH_PATTERN}$`).test(value)) return value;
        return MONTH_CODES[value.slice(0, 3)] || '';
    }

    function expandContractYear(value, referenceYear) {
        const text = String(value == null ? '' : value).trim();
        if (!/^\d{1,4}$/.test(text)) return null;
        const number = Number(text);
        if (text.length === 4) return number;
        if (text.length === 2) return number >= 70 ? 1900 + number : 2000 + number;
        if (text.length === 1) {
            const reference = asFinite(referenceYear) || new Date().getFullYear();
            const decade = Math.floor(reference / 10) * 10;
            let candidate = decade + number;
            if (candidate < reference - 5) candidate += 10;
            if (candidate > reference + 5) candidate -= 10;
            return candidate;
        }
        return null;
    }

    function parseTicker(value, options) {
        const settings = options && typeof options === 'object' ? options : {};
        const raw = String(value == null ? '' : value).trim();
        if (!raw) return null;
        let yellowKey = '';
        let contract = raw;
        const configuredKey = String(settings.yellow_key || settings.yellowKey || '').trim();
        if (configuredKey) {
            const escaped = configuredKey.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
            const configuredMatch = contract.match(new RegExp(`\\s+(${escaped})$`, 'i'));
            if (configuredMatch) {
                yellowKey = configuredMatch[1];
                contract = contract.slice(0, configuredMatch.index).trim();
            }
        }
        if (!yellowKey) {
            const genericMatch = contract.match(/\s+(Comdty|Index|Equity|Curncy)$/i);
            if (genericMatch) {
                yellowKey = genericMatch[1];
                contract = contract.slice(0, genericMatch.index).trim();
            }
        }
        contract = contract.replace(/[\s_-]+/g, '');
        const rootHint = String(settings.root || settings.root_hint || settings.rootHint || '').trim().toUpperCase();
        let match = null;
        if (rootHint && contract.toUpperCase().startsWith(rootHint)) {
            const tail = contract.slice(rootHint.length);
            const tailMatch = tail.match(new RegExp(`^(${CONTRACT_MONTH_PATTERN})(\\d{1,4})$`, 'i'));
            if (tailMatch) match = [contract, rootHint, tailMatch[1], tailMatch[2]];
        }
        if (!match) {
            match = contract.match(new RegExp(`^(.*?)(${CONTRACT_MONTH_PATTERN})(\\d{1,4})$`, 'i'));
        }
        if (!match || !match[1]) return null;
        const monthCode = match[2].toUpperCase();
        const yearDigits = match[3];
        const year = expandContractYear(yearDigits, settings.reference_year || settings.referenceYear);
        if (!year) return null;
        return {
            raw,
            root: match[1].toUpperCase(),
            monthCode,
            month: MONTH_NAMES[monthCode] || null,
            year,
            yearDigits,
            yellowKey
        };
    }

    function applyTickerTemplate(template, values) {
        return String(template || '')
            .replace(/\{([a-zA-Z0-9_]+)\}/g, function (match, key) {
                return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : match;
            })
            .replace(/\s+/g, ' ')
            .trim();
    }

    function buildTicker(rootCode, month, year, config) {
        const settings = config && typeof config === 'object' ? config : {};
        const rootValue = String(rootCode || settings.root || settings.security_root || '').trim().toUpperCase();
        const monthCode = monthToCode(month);
        const fullYear = expandContractYear(year, settings.reference_year || settings.referenceYear);
        if (!rootValue || !monthCode || !fullYear) return '';
        const yellowKey = String(settings.yellow_key || settings.yellowKey || '').trim();
        const yy = String(fullYear).slice(-2).padStart(2, '0');
        const y = yy;
        const values = {
            root: rootValue,
            security_root: rootValue,
            month: monthCode,
            month_code: monthCode,
            contract_month_code: monthCode,
            year: String(fullYear),
            yyyy: String(fullYear),
            y,
            year_1d: y,
            year_digit: y,
            yy,
            year2: yy,
            year_2d: yy,
            year_short: yy,
            yellow_key: yellowKey,
            yellowKey
        };
        const template = settings.ticker_template || settings.tickerTemplate || '{root}{month_code}{yy} {yellow_key}';
        return applyTickerTemplate(template, values).replace(/\s+$/, '');
    }

    return Object.freeze({
        MAX_DECIMALS,
        DEFAULT_INTERPOLATION_MAX_SPAN_DAYS,
        DEFAULT_BBL_PER_MT,
        DEFAULT_GAL_PER_BBL,
        SOURCE_AXIS_SPAN,
        CONTRACT_AXIS_SPAN,
        MONTH_CODES,
        MONTH_NAMES,
        asFinite,
        sourceCycleDay,
        toContractCycleDay,
        normalizeContractSeries,
        latestContractEndDay,
        rotateCycleDay,
        contractCycleAnchorDay,
        sourceCycleWindow,
        alignCycleStart,
        round,
        round5,
        format,
        normalizeUnit,
        normalizeCurveMode,
        rollMonthForPeriod,
        alignFlatCurveSeries,
        normalizeConversionConfig,
        convertValue,
        conversionFactor,
        weightedSum,
        interpolateSeriesAtTargets,
        interpolateInteriorSeries,
        longestContinuousSeries,
        rebaseSeriesYears,
        mergeReferenceSeries,
        clipSeriesToSourceCycle,
        contractYearForOffset,
        combineWeightedSeries,
        monthToCode,
        expandContractYear,
        parseTicker,
        applyTickerTemplate,
        buildTicker
    });
}));
