/**
 * EChartsRenderer — mounts a full Apache ECharts option on a canvas/SVG
 * surface inside the chat ReportCard.
 *
 * The agent (backend) emits `payload.chart = { engine: "echarts", option: {...} }`
 * and this component renders that option verbatim with the bundled,
 * self-contained ECharts 6.1.0 build (see frontend/vendor/echarts — no CDN,
 * no network access required, safe for the sandboxed preview constraints).
 *
 * Props:
 *   option  — a valid ECharts option object (required)
 *   height  — pixel height of the chart container (default 260)
 *   notMerge— replace the whole option on update instead of merging (default true)
 */

import { useEffect, useRef } from 'react';
import * as echarts from 'echarts';

export default function EChartsRenderer({ option, height = 260, notMerge = true }) {
  const containerRef = useRef(null);
  const chartRef = useRef(null);

  // Mount / dispose the instance once.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return undefined;

    const instance = echarts.init(el, null, { renderer: 'canvas' });
    chartRef.current = instance;

    const resize = () => instance.resize();
    const observer = new ResizeObserver(resize);
    observer.observe(el);

    return () => {
      observer.disconnect();
      instance.dispose();
      chartRef.current = null;
    };
  }, []);

  // Apply option updates without re-initialising.
  useEffect(() => {
    const instance = chartRef.current;
    if (!instance || !option) return;
    instance.setOption(option, { notMerge });
  }, [option, notMerge]);

  if (!option || typeof option !== 'object' || Array.isArray(option)) {
    return (
      <div
        className="flex w-full items-center justify-center rounded-lg border border-dashed border-border bg-secondary/20 text-xs text-muted-foreground"
        style={{ height }}
      >
        Invalid ECharts option
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      data-echarts-renderer="true"
      className="w-full"
      style={{ height, minHeight: 120 }}
    />
  );
}
