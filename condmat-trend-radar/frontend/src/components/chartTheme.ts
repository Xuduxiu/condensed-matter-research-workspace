import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react';
import * as echarts from 'echarts/core';
import type { EChartsCoreOption } from 'echarts/core';

const tokenNames = ['surface', 'surface-elevated', 'border', 'border-strong', 'text-primary', 'text-secondary',
  'text-tertiary', 'accent', 'success', 'warning', 'danger', 'preprint', 'publication', 'hot'] as const;

function readPalette() {
  const style = getComputedStyle(document.documentElement);
  // ECharts interpolates RGB colors itself. Let the browser resolve CSS color-mix,
  // modern color spaces and translucent glass tokens before passing them to the canvas.
  const canvas = document.createElement('canvas');
  canvas.width = tokenNames.length;
  canvas.height = 1;
  const context = canvas.getContext('2d', { willReadFrequently: true })!;
  tokenNames.forEach((name, i) => {
    context.fillStyle = style.getPropertyValue('--' + name).trim();
    context.fillRect(i, 0, 1, 1);
  });
  const pixels = context.getImageData(0, 0, tokenNames.length, 1).data;
  const token = Object.fromEntries(tokenNames.map((name, i) => {
    const [r, g, b, a] = pixels.subarray(i * 4, i * 4 + 4);
    return [name, 'rgba(' + [r, g, b, a / 255].join(',') + ')'];
  })) as Record<typeof tokenNames[number], string>;
  const axisText = token['text-secondary'];
  const bodyText = token['text-primary'];
  const gridLine = token.border;
  const colors = Array.from(new Set([token.preprint, token.publication, token.accent, token.success,
    token.warning, token.danger, token.hot, token['text-tertiary']]));
  const heatScale = [token['surface-elevated'], token.preprint, token.accent, token.hot];
  const tooltip = {
    className: 'desk-chart-tooltip',
    backgroundColor: token.surface,
    borderColor: token['border-strong'],
    textStyle: { color: bodyText, fontSize: 11 },
  };
  function categoryAxis(data: string[], rotate = 0) {
    return {
      type: 'category' as const, data,
      nameTextStyle: { color: axisText },
      axisLabel: { color: axisText, rotate, hideOverlap: true },
      axisLine: { lineStyle: { color: gridLine } },
      axisTick: { show: false },
      splitArea: { areaStyle: { color: [token.surface, token['surface-elevated']] } },
    };
  }
  function valueAxis(name?: string) {
    return {
      type: 'value' as const, name, minInterval: 1,
      nameTextStyle: { color: axisText }, axisLabel: { color: axisText },
      axisLine: { lineStyle: { color: gridLine } }, splitLine: { lineStyle: { color: gridLine } },
    };
  }
  return { token, colors, heatScale, bodyText, axisText, gridLine, tooltip, categoryAxis, valueAxis };
}

export type ChartPalette = ReturnType<typeof readPalette>;
export const ChartPaletteContext = createContext<ChartPalette | null>(null);

/** One subscription for the complete statistics page, including same-name host palette edits. */
export function useChartPalette() {
  const [palette, setPalette] = useState(readPalette);
  useLayoutEffect(() => {
    const refresh = () => setPalette(readPalette());
    window.addEventListener('dsh-paper-desk:themechange', refresh);
    refresh();
    return () => window.removeEventListener('dsh-paper-desk:themechange', refresh);
  }, []);
  return palette;
}

export function useChart(makeOption: ((palette: ChartPalette) => EChartsCoreOption) | null, onClick?: (params: unknown) => void) {
  const palette = useContext(ChartPaletteContext);
  const ref = useRef<HTMLDivElement>(null);
  const instance = useRef<echarts.EChartsType | null>(null);
  const pointer = useRef<{ x: number; y: number } | null>(null);
  const lastPalette = useRef<ChartPalette | null>(null);
  const visible = makeOption !== null;
  useEffect(() => {
    if (!visible || !ref.current) return;
    const chart = echarts.init(ref.current);
    instance.current = chart;
    chart.getZr().on('mousemove', event => { pointer.current = { x: event.offsetX, y: event.offsetY }; });
    chart.getZr().on('globalout', () => { pointer.current = null; });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => {
      observer.disconnect();
      instance.current = null;
      pointer.current = null;
      chart.dispose();
    };
  }, [visible]);
  useEffect(() => {
    const chart = instance.current;
    if (!makeOption || !palette || !chart) return;
    const previousTip = lastPalette.current !== palette && pointer.current
      ? chart.getDom().querySelector<HTMLElement>('.desk-chart-tooltip') : null;
    const tipPoint = previousTip && getComputedStyle(previousTip).visibility === 'visible' ? pointer.current : null;
    chart.setOption({
      color: palette.colors,
      textStyle: { color: palette.bodyText },
      axisPointer: {
        label: { color: palette.bodyText, backgroundColor: palette.token['surface-elevated'], borderColor: palette.gridLine },
        lineStyle: { color: palette.axisText },
        shadowStyle: { color: palette.axisText, opacity: 0.12 },
      },
      ...makeOption(palette),
    }, { replaceMerge: ['series'] });
    lastPalette.current = palette;
    if (tipPoint) {
      // ECharts 5 caches axis tooltip HTML by data index. Invalidate that entry
      // so a stationary pointer also gets new text colors and series markers.
      chart.dispatchAction({ type: 'hideTip' });
      chart.dispatchAction({ type: 'showTip', ...tipPoint });
    }
  }, [makeOption, palette]);
  useEffect(() => {
    const chart = instance.current;
    if (!chart || !onClick) return;
    chart.on('click', onClick);
    return () => { if (!chart.isDisposed()) chart.off('click', onClick); };
  }, [onClick, visible]);
  return ref;
}
