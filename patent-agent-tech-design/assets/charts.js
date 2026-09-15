(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg = style.getPropertyValue('--bg').trim();
  var accentSoft = style.getPropertyValue('--accent-soft').trim();
  var accent2Soft = style.getPropertyValue('--accent2-soft').trim();

  /* ---------- Mermaid 初始化 ---------- */
  if (window.mermaid) {
    mermaid.initialize({
      startOnLoad: true,
      theme: 'base',
      securityLevel: 'loose',
      themeVariables: {
        background: bg,
        primaryColor: accentSoft,
        primaryTextColor: ink,
        primaryBorderColor: accent,
        secondaryColor: accent2Soft,
        secondaryTextColor: ink,
        secondaryBorderColor: accent2,
        tertiaryColor: bg,
        lineColor: muted,
        textColor: ink,
        mainBkg: accentSoft,
        nodeBorder: accent,
        clusterBkg: bg,
        clusterBorder: rule,
        titleColor: ink,
        edgeLabelBackground: bg,
        nodeTextColor: ink,
        fontFamily: "'InstrumentSans', 'PingFang SC', 'Microsoft YaHei', sans-serif",
        fontSize: '14px'
      },
      flowchart: { curve: 'basis', htmlLabels: true },
      sequence: {
        actorBkg: accentSoft,
        actorBorder: accent,
        actorTextColor: ink,
        signalColor: muted,
        signalTextColor: ink,
        labelBoxBkgColor: accent2Soft,
        labelBoxBorderColor: accent2,
        labelTextColor: ink,
        loopTextColor: ink,
        noteBkgColor: accent2Soft,
        noteBorderColor: accent2,
        noteTextColor: ink
      }
    });
  }

  /* ---------- 图表：行业参考 · 两阶段检索召回基线 ---------- */
  var el = document.getElementById('chart-retrieval-baseline');
  if (el && window.echarts) {
    var chart = echarts.init(el, null, { renderer: 'svg' });
    chart.setOption({
      animation: false,
      grid: { left: 64, right: 32, top: 36, bottom: 48 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        appendToBody: true,
        formatter: function (params) {
          var p = params[0];
          return p.name + '<br/>Recall@5：' + Number(p.value).toFixed(3);
        }
      },
      xAxis: {
        type: 'category',
        data: ['混合检索\n（BM25 + 向量）', '混合检索 + Cross-Encoder 重排'],
        axisLine: { lineStyle: { color: rule } },
        axisTick: { show: false },
        axisLabel: { color: ink, fontSize: 12, lineHeight: 18, interval: 0 }
      },
      yAxis: {
        type: 'value',
        min: 0.6,
        max: 0.9,
        interval: 0.05,
        axisLabel: { color: muted, fontSize: 12 },
        splitLine: { lineStyle: { color: rule } }
      },
      series: [{
        type: 'bar',
        barWidth: '42%',
        data: [
          { value: 0.695, itemStyle: { color: accent2 } },
          { value: 0.816, itemStyle: { color: accent } }
        ],
        label: {
          show: true,
          position: 'top',
          color: ink,
          fontWeight: 600,
          formatter: function (p) { return p.value.toFixed(3); }
        }
      }]
    });
    window.addEventListener('resize', function () { chart.resize(); });
  }
})();
