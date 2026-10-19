// uPlot wrapper: the primary instrument on the Live screen.
//
// The design caps a single chart at 6 series (beyond that we switch to small
// multiples) because 8+ lines on one axis is "dashboard soup". A chart never
// interpolates across a drop: the caller pushes explicit points and a missed
// window simply leaves a gap.

import { h, clear } from "./dom.js";
import { seriesColorValue } from "./format.js";

// Resolve a design token to a concrete value. uPlot draws onto a canvas, which
// cannot resolve `var(--…)`, so the value has to be read out of the document.
// Never pass a colour as the fallback: the palette lives in app.css only, and
// tests/test_design_consistency.py fails on a colour literal in any JS file.
export function themeToken(name, fallback = "") {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

const MAX_POINTS = 4000;

// Series 5-8 sit close together under deuteranopia, so they are always drawn
// dashed as well as coloured (colour is never the only channel).
const DASHES = [[6, 3], [2, 3], [8, 3, 2, 3], [1, 3]];

export class TimeSeriesChart {
  constructor(container, { height = 320, yLabel = "" } = {}) {
    this.container = container;
    // A container that used to hold small multiples must not keep that grid.
    container.classList?.remove("small-multiples");
    this.height = height;
    this.yLabel = yLabel;
    this.series = [];      // [{key,label,unit,visible}]
    this.data = [[]];      // [xs, ...ys]
    this.plot = null;
    this.bookmarks = [];
    this._onTheme = () => this.rebuild();
    document.addEventListener("themechange", this._onTheme);
  }

  get containerNode() { return this.container; }

  setSeries(series) {
    const changed = series.length !== this.series.length
      || series.some((s, i) => s.key !== this.series[i]?.key);
    this.series = series.map((s, i) => ({ ...s, colour: s.colour || seriesColorValue(i), visible: true }));
    if (changed) this.rebuild();
    this.renderLegend();
  }

  update(payload) {
    if (!this.plot) this.ensurePlot();
    if (!this.plot) return;
    // Self-healing columns: `update` may be called before/after a series change
    // without a rebuild in between (a live chart configured after the first
    // sample), and pushing into a missing column threw instead of plotting.
    while (this.data.length < this.series.length + 1) this.data.push([]);
    const xs = this.data[0];
    xs.push(payload.t / 1000);
    this.series.forEach((serie, index) => {
      const row = this.data[index + 1];
      const entry = payload.values?.[serie.key];
      const value = entry && typeof entry.value === "number" ? entry.value : null;
      row.push(value);
    });
    if (xs.length > MAX_POINTS) {
      const drop = xs.length - MAX_POINTS;
      for (const row of this.data) row.splice(0, drop);
    }
    this.plot.setData(this.data);
  }

  setRows(rows) {
    // rows: {xs: [ms...], series: [{values: [...]}]} — one entry per series key.
    this.data = [rows.xs.map((t) => t / 1000), ...rows.series.map((s) => s.values)];
    // Rebuild so the plot is constructed *with* the data and draws synchronously
    // (uPlot paints its first frame on construction; later setData calls are
    // scheduled on requestAnimationFrame).
    this.rebuild();
    if (this.plot && this.data[0].length) {
      this.plot.setScale("x", { min: this.data[0][0], max: this.data[0][this.data[0].length - 1] });
    }
  }

  clear() {
    this.data = [[], ...this.series.map(() => [])];
    if (this.plot) this.plot.setData(this.data);
  }

  bookmark(tSeconds) {
    this.bookmarks.push(tSeconds);
    if (this.plot) this.plot.redraw();
  }

  clearBookmarks() {
    this.bookmarks = [];
    if (this.plot) this.plot.redraw();
  }

  toggle(key) {
    const serie = this.series.find((s) => s.key === key);
    if (!serie) return;
    serie.visible = !serie.visible;
    if (this.plot) {
      const index = this.series.indexOf(serie) + 1;
      this.plot.setSeries(index, { show: serie.visible });
    }
    this.renderLegend();
  }

  renderLegend() {
    const legend = this.container.parentElement?.querySelector(".legend");
    if (!legend) return;
    clear(legend);
    for (const serie of this.series) {
      legend.appendChild(h("button.legend__item", {
        type: "button", "aria-pressed": serie.visible ? "true" : "false",
        onClick: () => this.toggle(serie.key),
      },
        h("span.legend__chip", { style: { background: serie.colour } }),
        h("span", { text: serie.label }),
        serie.unit ? h("span.sub", { text: ` ${serie.unit}` }) : null));
    }
  }

  rebuild() {
    if (this.plot) { this.plot.destroy(); this.plot = null; }
    this.ensurePlot();
    if (this.plot) this.plot.setData(this.data);
  }

  ensurePlot() {
    if (this.plot || !this.series.length || typeof window.uPlot !== "function") return;
    const grid = themeToken("--chart-grid");
    const axis = themeToken("--chart-axis");
    const border = themeToken("--border");
    const font = themeToken("--font-sans", "sans-serif");
    const self = this;
    const axisBase = (extra) => ({
      stroke: axis, font: `11px ${font}`, labelFont: `11px ${font}`,
      grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1, size: 6 },
      border: { stroke: border, width: 1 }, ...extra,
    });
    const options = {
      width: this.container.clientWidth || 720,
      height: this.height,
      padding: [14, 10, 0, 0],
      series: [
        { label: "time" },
        ...this.series.map((serie, index) => ({
          label: serie.label,
          stroke: () => self.series.find((s) => s.key === serie.key)?.colour || serie.colour,
          width: 2,
          points: { show: false },
          dash: index >= 4 ? DASHES[index - 4] : undefined,
          value: (u, value) => (value === null || value === undefined
            ? "—" : `${Math.round(value * 100) / 100}${serie.unit ? " " + serie.unit : ""}`),
        })),
      ],
      scales: {
        x: { time: true },
        y: { auto: true },
      },
      axes: [
        axisBase({ grid: { stroke: grid, width: 1, show: true } }),
        axisBase({ label: this.yLabel, labelSize: 20, size: 54 }),
      ],
      cursor: {
        show: true, points: { show: true, size: 6, width: 1 },
        drag: { x: true, y: false, setScale: true },
      },
      focus: { alpha: 0.14 },
      legend: { show: false },
      plugins: [self._bookmarkPlugin()],
      hooks: {
        setSeries: [(u, index) => { if (index != null) { const s = self.series[index - 1]; if (s) s.visible = u.series[index].show; } }],
      },
    };
    this.dom = h("div.chart");
    clear(this.container);
    this.container.appendChild(this.dom);
    this.plot = new window.uPlot(options, this.data, this.dom);
  }

  _bookmarkPlugin() {
    const self = this;
    return {
      hooks: {
        draw: (u) => {
          if (!self.bookmarks.length) return;
          const ctx = u.ctx;
          ctx.save();
          ctx.strokeStyle = themeToken("--signal");
          ctx.setLineDash([4, 4]);
          ctx.lineWidth = 1;
          const top = u.bbox.top;
          const height = u.bbox.height;
          for (const t of self.bookmarks) {
            const x = Math.round(u.valToPos(t, "x", true));
            if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
            ctx.beginPath();
            ctx.moveTo(x, top);
            ctx.lineTo(x, top + height);
            ctx.stroke();
          }
          ctx.restore();
        },
      },
    };
  }

  resize() {
    if (this.plot) this.plot.setSize({ width: this.container.clientWidth || 720, height: this.height });
  }

  destroy() {
    document.removeEventListener("themechange", this._onTheme);
    if (this.plot) { this.plot.destroy(); this.plot = null; }
  }
}

/* Small multiples: one chart per series, used when a selection exceeds 6 signals. */
export function smallMultiples(container, series) {
  clear(container);
  container.classList.add("small-multiples");
  const charts = [];
  for (const serie of series) {
    const wrap = h("div.small-multiple", null,
      h("div.eyebrow", { text: `${serie.label}${serie.unit ? " (" + serie.unit + ")" : ""}` }),
      h("div.legend"), h("div.chart-holder"));
    container.appendChild(wrap);
    const chart = new TimeSeriesChart(wrap.querySelector(".chart-holder"), { height: 140 });
    chart.setSeries([serie]);
    charts.push(chart);
  }
  return charts;
}
