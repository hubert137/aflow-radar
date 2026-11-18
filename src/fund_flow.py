#!/usr/bin/env python3
"""
资金流向实时走势图
实时采集板块资金流数据，绘制多板块资金净流入走势图。
用法:
  python fund_flow.py              # 实时采集 + HTTP 实时图表（默认）
  python fund_flow.py --once       # 单次采集生成静态图表
  python fund_flow.py --collect    # 仅后台采集数据
  python fund_flow.py --chart      # 仅从已有数据生成图表
"""

import argparse
import http.server
import json
import re
import socketserver
import sys
import threading
import time
import webbrowser
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path

import requests
import pandas as pd
import plotly.graph_objects as go

try:
    import py_mini_racer
    from akshare.stock_feature.stock_fund_flow import _get_file_content_ths
    HAS_THS_AUTH = True
except ImportError:
    HAS_THS_AUTH = False

DATA_DIR = Path(__file__).parent / "data"
CHART_DIR = Path(__file__).parent / "charts"
COLLECT_INTERVAL = 300

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<title>A-Flow Radar</title>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
{refresh}
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ height: 100%; overflow: hidden; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Microsoft YaHei", "PingFang SC", sans-serif; }}
#chart-container {{ width: 100%; height: 100%; }}
</style>
<script src="/static/plotly.min.js"></script>
</head>
<body>
<div id="chart-container">{plot_html}</div>
</body>
</html>"""

DUAL_TAB_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<title>A-Flow Radar</title>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
{refresh}
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ height: 100%; overflow: hidden; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, "Microsoft YaHei", "PingFang SC", sans-serif; background: #f5f5f5; }}
.tab-bar {{
    display: flex; align-items: center; gap: 0;
    background: #fff; border-bottom: 2px solid #e0e0e0;
    padding: 0 16px; height: 44px; flex-shrink: 0;
}}
.tab-btn {{
    padding: 8px 24px; font-size: 14px; font-weight: 600;
    border: none; background: none; cursor: pointer;
    color: #888; border-bottom: 3px solid transparent;
    transition: all 0.2s; margin-bottom: -2px;
}}
.tab-btn:hover {{ color: #333; }}
.tab-btn.active {{ color: #1a73e8; border-bottom-color: #1a73e8; }}
.tab-badge {{
    font-size: 11px; color: #999; margin-left: 4px; font-weight: 400;
}}
.tab-panel {{ display: none; width: 100%; height: calc(100vh - 44px); }}
.tab-panel.active {{ display: block; }}
.page-wrap {{ display: flex; flex-direction: column; height: 100vh; }}
</style>
<script src="/static/plotly.min.js"></script>
</head>
<body>
<div class="page-wrap">
  <div class="tab-bar">
    <button class="tab-btn active" onclick="switchTab('em')">东方财富<span class="tab-badge">主力净流入</span></button>
    <button class="tab-btn" onclick="switchTab('ths')">同花顺<span class="tab-badge">资金净额</span></button>
  </div>
  <div id="panel-em" class="tab-panel active">{em_chart}</div>
  <div id="panel-ths" class="tab-panel">{ths_chart}</div>
</div>
<script>
function switchTab(tab) {{
    document.querySelectorAll('.tab-btn').forEach(function(btn, i) {{
        btn.classList.toggle('active', (tab === 'em' && i === 0) || (tab === 'ths' && i === 1));
    }});
    document.getElementById('panel-em').classList.toggle('active', tab === 'em');
    document.getElementById('panel-ths').classList.toggle('active', tab === 'ths');
    // Trigger Plotly resize for the newly visible chart
    var panel = document.getElementById('panel-' + tab);
    var gd = panel.querySelector('.js-plotly-plot');
    if (gd) {{ Plotly.Plots.resize(gd); }}
}}
</script>
</body>
</html>"""

# AJAX 动态模板：JS 轮询 /api/data，Plotly.react 增量更新
AJAX_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<title>A-Flow Radar</title>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { height: 100%; overflow: hidden; }
body { font-family: -apple-system, BlinkMacSystemFont, "Microsoft YaHei", "PingFang SC", sans-serif; background: #f5f5f5; }
.tab-bar {
    display: flex; align-items: center; gap: 0;
    background: #fff; border-bottom: 2px solid #e0e0e0;
    padding: 0 16px; height: 44px; flex-shrink: 0;
}
.tab-btn {
    padding: 8px 24px; font-size: 14px; font-weight: 600;
    border: none; background: none; cursor: pointer;
    color: #888; border-bottom: 3px solid transparent;
    transition: all 0.2s; margin-bottom: -2px;
}
.tab-btn:hover { color: #333; }
.tab-btn.active { color: #1a73e8; border-bottom-color: #1a73e8; }
.tab-badge { font-size: 11px; color: #999; margin-left: 4px; font-weight: 400; }
.status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    display: inline-block; margin-left: 8px; vertical-align: middle;
}
.status-dot.ok { background: #2ecc71; }
.status-dot.loading { background: #f39c12; animation: pulse 1s infinite; }
.status-dot.error { background: #e74c3c; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
.sub-tab-bar {
    display: flex; align-items: center; gap: 0;
    background: #fafafa; border-bottom: 1px solid #e0e0e0;
    padding: 0 16px; height: 38px; flex-shrink: 0;
}
.sub-tab-btn {
    padding: 6px 20px; font-size: 13px; font-weight: 500;
    border: none; background: none; cursor: pointer;
    color: #888; border-bottom: 2px solid transparent;
    transition: all 0.2s; margin-bottom: -1px;
}
.sub-tab-btn:hover { color: #333; }
.sub-tab-btn.active { color: #1a73e8; border-bottom-color: #1a73e8; font-weight: 600; }
.tab-panel { display: none; width: 100%; height: calc(100vh - 82px); }
.tab-panel.active { display: block; }
.page-wrap { display: flex; flex-direction: column; height: 100vh; }
.chart-box { width: 100%; height: 100%; }
.loading-msg {
    display: flex; align-items: center; justify-content: center;
    height: 100%; color: #999; font-size: 14px;
}
.date-picker {
    margin-left: auto; display: flex; align-items: center; gap: 6px;
}
.date-picker label { font-size: 12px; color: #888; }
.date-picker input[type=date] {
    padding: 4px 8px; font-size: 13px; border: 1px solid #ddd;
    border-radius: 4px; font-family: inherit; color: #333;
}
.date-picker button {
    padding: 4px 10px; font-size: 12px; border: 1px solid #ddd;
    border-radius: 4px; background: #fff; cursor: pointer; color: #555;
}
.date-picker button:hover { background: #f0f0f0; }

html, body { height: auto; overflow: auto; }
.page-wrap { height: auto; min-height: 100vh; }
.tab-bar { position: sticky; top: 0; z-index: 5; }
.sub-tab-bar { height: 38px; color: #555; font-size: 13px; }
#panel-em-all, #panel-em-in, #panel-em-out { display: block !important; height: 520px; flex-shrink: 0; border-bottom: 8px solid #f0f2f5; }
#panel-ths-in, #panel-ths-out { display: none !important; }
button[onclick="switchTab('ths')"] { display: none; }
@media (max-width: 760px) { #panel-em-all, #panel-em-in, #panel-em-out { height: 480px; } .date-picker label { display: none; } .tab-bar { padding: 0 6px; } .tab-btn { padding: 8px; } }
</style>
<script src="/static/plotly.min.js"></script>
</head>
<body>
<div class="page-wrap">
  <div class="tab-bar">
    <button class="tab-btn active" onclick="switchTab('em')">
      东方财富<span class="tab-badge">主力净流入</span>
      <span class="status-dot loading" id="dot-em"></span>
    </button>
    <button class="tab-btn" onclick="switchTab('ths')">
      同花顺<span class="tab-badge">未启用</span>
      <span class="status-dot loading" id="dot-ths"></span>
    </button>
    <div class="date-picker">
      <label>周期</label><select id="period-picker"><option value="today">今天</option><option value="week">本周</option><option value="month">本月</option><option value="year">本年</option></select><label>单日回看</label>
      <input type="date" id="date-picker" />
      <button onclick="jumpToToday()">今日</button>
    </div>
  </div>
  <div class="sub-tab-bar">
    <span>A-Flow Radar · 净额总览 → 净流入 → 净流出 · 每条线对应一个板块，不跨板块求和 · 每300秒采集 · 单位：亿元</span>
  </div>
  <div id="coverage-note" style="padding:8px 16px;color:#805700;background:#fff8e5"></div><div id="panel-em-all" class="tab-panel"><div id="chart-em-all" class="chart-box"><div class="loading-msg">加载中...</div></div></div>
  <div id="panel-em-in" class="tab-panel active"><div id="chart-em-in" class="chart-box"><div class="loading-msg">加载中...</div></div></div>
  <div id="panel-em-out" class="tab-panel"><div id="chart-em-out" class="chart-box"><div class="loading-msg">加载中...</div></div></div>
  <div id="panel-ths-in" class="tab-panel"><div id="chart-ths-in" class="chart-box"><div class="loading-msg">加载中...</div></div></div>
  <div id="panel-ths-out" class="tab-panel"><div id="chart-ths-out" class="chart-box"><div class="loading-msg">加载中...</div></div></div>
</div>
<script>
const POLL_INTERVAL = __POLL_MS__;
const COLORS = __COLORS_JSON__;
const OUTFLOW_COLORS = __OUTFLOW_JSON__;
let activeTab = 'em';
let activeSub = 'in';
let charts = { em: { in: null, out: null }, ths: { in: null, out: null } };
let selectedPeriod = 'today';
let selectedDate = null;  // null=今日，否则 YYYY-MM-DD

function showActivePanel() {
    var panels = ['em-in', 'em-out', 'ths-in', 'ths-out'];
    var active = activeTab + '-' + activeSub;
    panels.forEach(function(p) {
        document.getElementById('panel-' + p).classList.toggle('active', p === active);
    });
    var gd = document.getElementById('chart-' + active);
    if (gd && gd.classList.contains('js-plotly-plot')) {
        Plotly.Plots.resize(gd);
    }
}

function switchTab(tab) {
    activeTab = tab;
    document.querySelectorAll('.tab-btn').forEach(function(btn, i) {
        btn.classList.toggle('active', (tab === 'em' && i === 0) || (tab === 'ths' && i === 1));
    });
    showActivePanel();
}

function switchSub(sub) {
    activeSub = sub;
    document.querySelectorAll('.sub-tab-btn').forEach(function(btn, i) {
        btn.classList.toggle('active', (sub === 'in' && i === 0) || (sub === 'out' && i === 1));
    });
    showActivePanel();
}

function setDot(source, status) {
    var dot = document.getElementById('dot-' + source);
    if (dot) { dot.className = 'status-dot ' + status; }
}

function showNoData(source, direction, label) {
    var el = document.getElementById('chart-' + source + '-' + direction);
    var dir = (direction === 'out') ? '净流出' : '净流入';
    if (el) { el.innerHTML = '<div class="loading-msg">暂无' + label + dir + '数据</div>'; }
}

function buildTraces(sectors, markerSize) {
    return sectors.map(function(s) {
        var cfg = {
            x: s.times,
            y: s.values,
            mode: 'lines+markers',
            name: s.name,
            line: { color: s.color, width: 2, dash: s.is_outflow ? 'dash' : 'solid' },
            marker: { color: s.color, size: markerSize },
            hovertemplate: '<b>' + s.name + '</b><br>时间: %{x|%Y-%m-%d %H:%M:%S}<br>' + (s.is_outflow ? '资金净额（负值表示净流出）' : s.y_label) + ': %{y:.2f}亿<extra></extra>',
            hoverlabel: { namelength: -1, font_size: 13 }
        };
        if (s.is_outflow) {
            cfg.marker.symbol = 'diamond';
        }
        return cfg;
    });
}

function declutterY(items, yMin, yMax) {
    // items: [{i, y}] —— 按期望 y 自上而下贪心排布，保证不重叠且不越出 [yMin, yMax]
    // 返回与 items 同序的调整后 y 数组
    if (!items.length) return [];
    var span = yMax - yMin;
    var n = items.length;
    var gap = (span > 0 && n > 0) ? 0.9 * span / n : 1.0;
    var order = items.slice().sort(function(a, b) { return b.y - a.y; });
    var placed = {};
    var prevY = null;
    order.forEach(function(it) {
        var y = (prevY === null) ? Math.min(it.y, yMax) : Math.min(it.y, prevY - gap);
        placed[it.i] = y;
        prevY = y;
    });
    var bottom = Math.min.apply(null, Object.keys(placed).map(function(k) { return placed[k]; }));
    if (bottom < yMin) {
        var shift = yMin - bottom;
        Object.keys(placed).forEach(function(k) { placed[k] += shift; });
    }
    return items.map(function(it) { return placed[it.i]; });
}

function buildAnnotations(sectors, y_label) {
    // 先求出所有数据点的 y 范围（近似 Plotly 自动量程），用于换算防重叠间距
    var yMin = Infinity, yMax = -Infinity;
    sectors.forEach(function(s) {
        s.values.forEach(function(v) {
            if (v == null) return;  // 增量模式下首点 delta 可能为 null
            if (v < yMin) yMin = v;
            if (v > yMax) yMax = v;
        });
    });
    if (!isFinite(yMin)) { yMin = 0; yMax = 1; }
    var pad = (yMax - yMin) * 0.05 || 1;
    yMin -= pad; yMax += pad;

    var items = sectors.map(function(s, i) {
        return { i: i, y: s.values[s.values.length - 1] };
    });
    var adjY = declutterY(items, yMin, yMax);

    var annots = [];
    sectors.forEach(function(s, i) {
        var lastVal = s.values[s.values.length - 1];
        var valStr = (lastVal >= 0 ? '+' : '') + lastVal.toFixed(2);
        if (Math.abs(lastVal) < 1) { valStr = (lastVal >= 0 ? '+' : '') + lastVal.toFixed(4); }
        annots.push({
            x: 1.01, y: adjY[i],
            xref: 'paper', yref: 'y',
            text: '<b>' + s.name + '</b> ' + valStr,
            showarrow: false,
            xanchor: 'left', yanchor: 'middle',
            font: { color: s.color, size: s.is_outflow ? 11 : 12, family: 'Microsoft YaHei, SimHei, sans-serif' },
            bgcolor: 'rgba(255,255,255,0.95)',
            bordercolor: s.color,
            borderwidth: 1.5,
            borderpad: 3
        });
    });
    return annots;
}

function buildLayout(data, divId, direction) {
    var dirLabel = direction === 'all' ? '净额总览（流入前30 / 流出前15，正负同图）' : ((direction === 'out') ? '净流出' : '净流入');
    return {
        title: {
            text: '<b>' + data.source_label + ' · 概念板块主力资金' + dirLabel + '走势图 ' + data.title_suffix + '</b>',
            font: { size: 16, color: '#333' },
            x: 0.01, y: 0.98, xanchor: 'left', yanchor: 'top'
        },
        xaxis: {
            title: '', type: 'date', dtick: data.period_view && data.time_points < 8 ? 86400000 : undefined, tickformat: data.period_view ? '%m-%d' : '%H:%M',
            showgrid: false, zeroline: false,
            range: data.x_range,
            rangebreaks: data.period_view ? [] : [{ bounds: [11.5, 13], pattern: 'hour' }],
            tickfont: { size: 11, color: '#666' }
        },
        yaxis: {
            title: { text: direction === 'all' ? '资金净额（亿元；正=净流入，负=净流出）' : (direction === 'out' ? '资金净额（亿元，负值表示净流出）' : data.y_label), font: { size: 12, color: '#666' } },
            type: data.log_scale ? 'log' : 'linear',
            showgrid: true, gridcolor: '#f0f0f0',
            zeroline: true, zerolinecolor: '#ccc', zerolinewidth: 1,
            tickfont: { size: 11, color: '#666' }
        },
        plot_bgcolor: '#fafafa',
        paper_bgcolor: 'white',
        hovermode: 'closest',
        hoverdistance: 20,
        legend: {
            orientation: 'h', yanchor: 'top', y: 0.99, xanchor: 'left', x: 0.01,
            font: { size: 10 }, bgcolor: 'rgba(255,255,255,0.8)',
            bordercolor: '#ddd', borderwidth: 1
        },
        margin: { l: 60, r: 30, t: 80, b: 40 },
        annotations: [{
            x: 0.995, y: 0.01, xref: 'paper', yref: 'paper',
            text: '更新于 ' + data.updated_at,
            showarrow: false, font: { size: 10, color: '#999' }
        }]
    };
}

function renderChart(source, data, direction) {
    var divId = 'chart-' + source + '-' + direction;
    var el = document.getElementById(divId);
    if (!el) return;
    var isOut = (direction === 'out');
    if (!data || !data.sectors || data.sectors.length === 0) {
        var dir = isOut ? '净流出' : '净流入';
        el.innerHTML = '<div class="loading-msg">暂无' + (data ? data.source_label : '') + dir + '数据</div>';
        return;
    }
    var sectors = data.sectors.filter(function(s) { return direction === 'all' || s.is_outflow === isOut; });
    if (sectors.length === 0) {
        var dirLbl = isOut ? '净流出' : '净流入';
        el.innerHTML = '<div class="loading-msg">暂无' + data.source_label + dirLbl + '数据</div>';
        return;
    }
    var timePoints = data.time_points || 10;
    var markerSize = timePoints <= 3 ? 6 : 3;
    var traces = buildTraces(sectors, markerSize);
    var subData = Object.assign({}, data, { sectors: sectors });
    var layout = buildLayout(subData, divId, direction);
    var config = { responsive: true, displayModeBar: true };

    if (charts[source][direction]) {
        Plotly.react(divId, traces, layout, config);
    } else {
        el.innerHTML = '';
        Plotly.newPlot(divId, traces, layout, config);
        charts[source][direction] = true;
    }
}

async function fetchData() {
    try {
        var url = '/api/data?period=' + selectedPeriod + (selectedDate ? ('&date=' + selectedDate) : '');
        var resp = await fetch(url);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        var json = await resp.json();
        document.getElementById('coverage-note').textContent = json.note || '';

        if (json.em) {
            setDot('em', 'ok');
            renderChart('em', json.em, 'all');
            renderChart('em', json.em, 'in');
            renderChart('em', json.em, 'out');
        } else {
            setDot('em', 'error');
            showNoData('em', 'all', '东方财富');
            showNoData('em', 'in', '东方财富');
            showNoData('em', 'out', '东方财富');
        }
        if (json.ths) {
            setDot('ths', 'ok');
            renderChart('ths', json.ths, 'in');
            renderChart('ths', json.ths, 'out');
        } else {
            setDot('ths', 'error');
            showNoData('ths', 'in', '同花顺');
            showNoData('ths', 'out', '同花顺');
        }
    } catch (e) {
        console.error('Fetch error:', e);
        setDot('em', 'error');
        setDot('ths', 'error');
    }
}

function todayStr() {
    return new Date().toLocaleDateString('sv-SE', {timeZone:'Asia/Shanghai'});
}

function resetCharts() {
    charts = { em: { in: null, out: null }, ths: { in: null, out: null } };
}

function onDateChange() {
    selectedPeriod = 'today'; document.getElementById('period-picker').value = 'today';
    var val = document.getElementById('date-picker').value;
    selectedDate = (val === todayStr()) ? null : val;
    resetCharts();
    fetchData();
}

function jumpToToday() {
    selectedPeriod = 'today'; document.getElementById('period-picker').value = 'today';
    document.getElementById('date-picker').value = todayStr();
    selectedDate = null;
    resetCharts();
    fetchData();
}

// 初始化日期选择器：默认今日，并加载可选日期范围
function initDatePicker() {
    var picker = document.getElementById('date-picker');
    picker.value = todayStr();
    picker.addEventListener('change', onDateChange);
    fetch('/api/dates').then(function(r) { return r.json(); }).then(function(d) {
        var all = (d.em || []).concat(d.ths || []);
        if (all.length) {
            all.sort();
            picker.min = all[0];
            picker.max = todayStr();
        }
    }).catch(function(){});
}

document.getElementById('period-picker').addEventListener('change', function() {
    selectedPeriod = this.value; selectedDate = null;
    document.getElementById('date-picker').value = todayStr();
    resetCharts(); fetchData();
});
// 启动
initDatePicker();
fetchData();
setInterval(fetchData, POLL_INTERVAL);
</script>
</body>
</html>"""


def _wrap_html(plot_html: str, refresh_seconds: int = None) -> str:
    refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds else ""
    return HTML_TEMPLATE.format(refresh=refresh, plot_html=plot_html)


def _wrap_dual_html(em_chart: str, ths_chart: str, refresh_seconds: int = None) -> str:
    refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds else ""
    return DUAL_TAB_TEMPLATE.format(refresh=refresh, em_chart=em_chart, ths_chart=ths_chart)
MARKET_OPEN = dt_time(9, 30)
MARKET_CLOSE = dt_time(15, 0)
LUNCH_START = dt_time(11, 30)
LUNCH_END = dt_time(13, 0)
TOP_N = 10
BOTTOM_N = 5  # 净流出前5名
HTTP_PORT = 8899
DATA_FILENAME = "fund_flow_{date}.json"
THS_DATA_FILENAME = "ths_fund_flow_{date}.json"

COLORS = [
    "#e74c3c", "#2ecc71", "#3498db", "#f39c12", "#9b59b6",
    "#1abc9c", "#e67e22", "#34495e", "#c0392b", "#16a085",
    "#2980b9", "#8e44ad", "#27ae60", "#d35400", "#2c3e50",
    "#e91e63", "#00bcd4", "#ff5722", "#795548", "#607d8b",
]
# 净流出板块配色（彼此区分明显；线型用虚线与净流入区分，故配色可自由取色）
OUTFLOW_COLORS = [
    "#1976d2",  # 蓝
    "#00838f",  # 青绿
    "#6a1b9a",  # 紫
    "#d81b60",  # 玫红
    "#ef6c00",  # 橙
]

# 过滤掉非行业/概念板块（市场机制/指数/风格/状态类），东方财富和同花顺共用
SECTOR_BLACKLIST = {
    # 市场机制
    "融资融券", "融资", "融券", "深股通", "沪股通", "北向资金", "南向资金",
    "央行票据", "国债逆回购", "证金持股",
    # 指数（沪深港/国际/编制类）
    "中证500", "中证1000", "沪深300", "HS300_", "上证50", "上证50_", "上证180_", "上证380",
    "央视50_", "深成500", "深证100R", "创业成份", "创业板综", "创业板指", "科创50",
    "MSCI", "MSCI中国", "富时罗素", "标普道琼斯中国", "标准普尔",
    "宁组合", "茅指数",
    "同花顺中特估100", "同花顺出海50", "同花顺新质50", "同花顺果指数", "同花顺漂亮100",
    # 风格/市值因子
    "先进制造风格", "医药医疗风格", "科技风格", "消费风格", "金融地产风格",
    "大盘价值", "大盘成长", "大盘股", "中盘价值", "中盘成长", "中盘股",
    "小盘价值", "小盘成长", "小盘股", "价值股", "周期股", "趋势股", "题材股",
    "反转股", "超跌股", "微利股", "红利股", "红利破净股", "长期破净",
    # 市场状态/股性
    "ST股", "百元股", "破净股", "破发股", "破增发价股", "次新股",
    "昨日涨停", "昨日涨停_含一字", "昨日炸板", "昨日触板", "昨日连板",
    "昨日连板_含一字", "昨日首板", "昨日高振幅", "昨日高换手", "昨日打二板以上表现",
    "最近多板", "近期新高", "百日新高", "历史新高",
    "举牌", "机构重仓", "基金重仓", "社保重仓", "QFII重仓", "权重股",
    "行业龙头", "独角兽", "东方财富热股",
    "转债标的", "科创板做市商", "科创板做市股", "IPO受益",
    "股权激励", "股权转让", "并购重组概念",
    # 参股类
    "参股保险", "参股券商", "参股新三板", "参股期货", "参股银行",
    # 股票类型
    "AB股", "AH股", "B股",
    # 改革/区域/持股主题（用户指定删除）
    "国家大基金持股", "央国企改革", "央企国企改革", "国企改革", "沪企改革",
    "海峡两岸", "粤港澳大湾区",
}

# 带年份等动态命名的板块用正则匹配（如 2025三季报预增）
SECTOR_BLACKLIST_PATTERNS = [
    re.compile(r"^20\d{2}(年报|半年报|中报|一季报|三季报|季报)"),
]


def is_blacklisted(name: str) -> bool:
    """判断板块名是否在黑名单（精确匹配或正则匹配）。"""
    if name in SECTOR_BLACKLIST:
        return True
    return any(p.search(name) for p in SECTOR_BLACKLIST_PATTERNS)


def _read_jsonl(fp: Path) -> pd.DataFrame:
    """逐行读取 JSONL，跳过损坏行（兼容采集中断留下的半行/null字节）。"""
    rows = []
    with open(fp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (json.JSONDecodeError, ValueError):
                continue
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def is_trading_day() -> bool:
    return datetime.now().weekday() < 5


def is_market_open() -> bool:
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE


def is_lunch_break() -> bool:
    """11:30-13:00 午休时段。"""
    now = datetime.now().time()
    return LUNCH_START <= now < LUNCH_END


def is_collecting_hours() -> bool:
    """采集时段：交易日 9:30-11:30 和 13:00-15:00。"""
    if not is_trading_day():
        return False
    now = datetime.now().time()
    morning = MARKET_OPEN <= now < LUNCH_START
    afternoon = LUNCH_END <= now <= MARKET_CLOSE
    return morning or afternoon


def market_session() -> str:
    """返回当前市场状态：trading / lunch / closed。"""
    if not is_trading_day():
        return "closed"
    now = datetime.now().time()
    if MARKET_OPEN <= now < LUNCH_START:
        return "trading"
    if LUNCH_START <= now < LUNCH_END:
        return "lunch"
    if LUNCH_END <= now <= MARKET_CLOSE:
        return "trading"
    return "closed"


def fmt_value(v: float) -> str:
    if abs(v) >= 1:
        return f"{v:+.2f}"
    return f"{v:+.4f}"


def _declutter_y(desired: list, y_min: float, y_max: float) -> list:
    """防重叠：把标签的期望 y 自上而下贪心排布，保证彼此间距 ≥ gap 且不越出 [y_min, y_max]。

    desired: [(key, y), ...]，返回 {key: 调整后 y}。
    数据自然分散时标签基本不动；只在收尾值聚集时才把重叠的标签向下推开。
    """
    if not desired:
        return {}
    span = y_max - y_min
    n = len(desired)
    gap = 0.9 * span / n if span > 0 and n > 0 else 1.0
    order = sorted(desired, key=lambda it: it[1], reverse=True)
    placed = {}
    prev_y = None
    for key, dy in order:
        y = min(dy, y_max) if prev_y is None else min(dy, prev_y - gap)
        placed[key] = y
        prev_y = y
    bottom = min(placed.values())
    if bottom < y_min:
        shift = y_min - bottom
        for k in placed:
            placed[k] += shift
    return placed


class FundFlowCollector:
    """基于东方财富 API 实时采集概念板块主力净流入数据。

    每个 tick 存储板块名、主力净流入累计值（亿）及增量值（亿）。
    前端可在"增量模式"和"累计模式"间切换：
    - 增量模式：观察资金节奏和转折点
    - 累计模式：看全天总结果
    """

    EAST_MONEY_URL = "https://data.eastmoney.com/dataapi/bkzj/getbkzj"
    EAST_MONEY_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com/bkzj/gn.html",
    }

    def __init__(self, save_dir: Path = DATA_DIR):
        self.save_dir = save_dir
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.running = False
        self._thread = None
        self._prev_flow: dict[str, float] = {}
        self._session = requests.Session()
        self._session.trust_env = False

    def _fetch_raw(self) -> list[dict]:
        r = self._session.get(
            self.EAST_MONEY_URL,
            params={"key": "f62", "code": "m:90+t:3"},
            headers=self.EAST_MONEY_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        rows = (data.get("data") or {}).get("diff", [])
        if data.get("rc") != 0 or not rows:
            raise ValueError("upstream response contains no valid data")
        return rows

    def _parse(self, raw: list[dict]) -> list[dict]:
        now = datetime.now()
        session_key = (now.strftime("%Y%m%d"), "am" if now.hour < 12 else "pm")
        if getattr(self, "_session_key", None) != session_key:
            self._prev_flow = {}
            self._session_key = session_key
        rows = []
        for item in raw:
            name = item.get("f14", "")
            if is_blacklisted(name):
                continue
            value = item.get("f62")
            if not name or not isinstance(value, (int, float)):
                continue
            main_net = value / 1e8  # 转为亿
            prev = self._prev_flow.get(name)
            delta = main_net - prev if prev is not None else None
            rows.append({
                "time": now.strftime("%H:%M:%S"),
                "sector": name,
                "main_net_inflow": main_net,
                "delta": delta,
                "change_pct": None,
                "index_value": None,
            })
        self._prev_flow = {r["sector"]: r["main_net_inflow"] for r in rows}
        return rows

    def snapshot(self) -> list[dict]:
        raw = self._fetch_raw()
        rows = self._parse(raw)
        if not rows:
            raise ValueError("no valid sectors")
        self._persist(rows)
        print(f"snapshot {datetime.now().isoformat()} sectors={len(rows)}", flush=True)
        return rows

    def _persist(self, rows: list[dict]):
        today = datetime.now().strftime("%Y%m%d")
        fp = self.save_dir / DATA_FILENAME.format(date=today)
        with open(fp, "a") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def start(self, interval: int = COLLECT_INTERVAL):
        if self.running:
            return
        self.running = True
        # automatic cleanup disabled

        def _loop():
            while self.running:
                started = time.monotonic()
                try:
                    if is_collecting_hours():
                        self.snapshot()
                    else:
                        session = market_session()
                        label = {"lunch": "午休时段", "closed": "非交易时间"}.get(session, "非交易时间")
                        print(f"\r[{datetime.now().strftime('%H:%M:%S')}] {label}，等待中...", end="")
                except Exception as e:
                    print(f"\n采集错误: {e}")
                time.sleep(max(0, interval - (time.monotonic() - started)))

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        print(f"数据采集已启动（间隔 {interval}s）")

    def _cleanup(self, keep_days: int = 30):
        """清理超过 keep_days 天的历史数据文件。"""
        cutoff = datetime.now() - timedelta(days=keep_days)
        cutoff_str = cutoff.strftime("%Y%m%d")
        removed = 0
        for fp in self.save_dir.glob("fund_flow_*.json"):
            date_str = fp.stem.replace("fund_flow_", "")
            if date_str <= cutoff_str:
                fp.unlink()
                removed += 1
        if removed:
            print(f"已清理 {removed} 个过期数据文件（保留近 {keep_days} 天）")

    def stop(self):
        self.running = False


class THSFundFlowCollector:
    """基于 akshare 实时采集同花顺概念板块资金流向数据。

    使用 stock_fund_flow_concept 接口获取全量板块数据（约 386 个），
    过滤黑名单后按净额排序取 Top/Bottom N。
    数据格式与 FundFlowCollector 一致，共用 FundFlowChart 生成图表。
    """

    def __init__(self, save_dir: Path = DATA_DIR):
        self.save_dir = save_dir
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.running = False
        self._thread = None
        self._prev_flow: dict[str, float] = {}

    def _fetch_raw(self) -> list[dict]:
        """使用 akshare 获取全量概念板块资金流向数据。"""
        try:
            from akshare.stock_feature.stock_fund_flow import stock_fund_flow_concept
            df = stock_fund_flow_concept(symbol="即时")
            # 转换为与旧接口兼容的格式
            results = []
            for _, row in df.iterrows():
                results.append({
                    "name": row["行业"],
                    "amount": float(row["净额"]),  # 净额（亿）
                    "inflow": float(row["流入资金"]),
                    "outflow": float(row["流出资金"]),
                    "change_pct": float(row["行业-涨跌幅"]) if pd.notna(row["行业-涨跌幅"]) else 0,
                })
            return results
        except Exception as e:
            print(f"同花顺 akshare 请求失败: {e}")
            return []

    def _parse(self, raw: list[dict]) -> list[dict]:
        now = datetime.now()
        rows = []
        for item in raw:
            name = item.get("name", "")
            if is_blacklisted(name):
                continue
            amount = item.get("amount", 0)  # 净额（亿）
            prev = self._prev_flow.get(name)
            delta = amount - prev if prev is not None else None
            rows.append({
                "time": now.strftime("%H:%M:%S"),
                "sector": name,
                "main_net_inflow": amount,
                "delta": delta,
                "change_pct": None,
                "index_value": None,
            })
        self._prev_flow = {r["sector"]: r["main_net_inflow"] for r in rows}
        return rows

    def snapshot(self) -> list[dict]:
        raw = self._fetch_raw()
        rows = self._parse(raw)
        self._persist(rows)
        return rows

    def _persist(self, rows: list[dict]):
        today = datetime.now().strftime("%Y%m%d")
        fp = self.save_dir / THS_DATA_FILENAME.format(date=today)
        with open(fp, "a") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def start(self, interval: int = COLLECT_INTERVAL):
        if self.running:
            return
        if not HAS_THS_AUTH:
            print("同花顺采集需要 py_mini_racer 和 akshare，跳过")
            return
        self.running = True
        # automatic cleanup disabled

        def _loop():
            while self.running:
                started = time.monotonic()
                try:
                    if is_collecting_hours():
                        self.snapshot()
                    else:
                        session = market_session()
                        label = {"lunch": "午休时段", "closed": "非交易时间"}.get(session, "非交易时间")
                        print(f"\r[{datetime.now().strftime('%H:%M:%S')}] [同花顺] {label}，等待中...", end="")
                except Exception as e:
                    print(f"\n[同花顺] 采集错误: {e}")
                time.sleep(max(0, interval - (time.monotonic() - started)))

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        print(f"[同花顺] 数据采集已启动（间隔 {interval}s）")

    def _cleanup(self, keep_days: int = 30):
        cutoff = datetime.now() - timedelta(days=keep_days)
        cutoff_str = cutoff.strftime("%Y%m%d")
        removed = 0
        for fp in self.save_dir.glob("ths_fund_flow_*.json"):
            date_str = fp.stem.replace("ths_fund_flow_", "")
            if date_str <= cutoff_str:
                fp.unlink()
                removed += 1
        if removed:
            print(f"[同花顺] 已清理 {removed} 个过期数据文件（保留近 {keep_days} 天）")

    def stop(self):
        self.running = False


class FundFlowChart:

    def load_data(self, data_path: Path = None, source: str = "em",
                  date: str = None) -> pd.DataFrame:
        if data_path is not None and data_path.suffix == ".csv":
            return pd.read_csv(data_path, parse_dates=["time"])
        today = datetime.now().strftime("%Y%m%d")
        if source == "ths":
            pattern = "ths_fund_flow_*.json"
            name_tmpl = THS_DATA_FILENAME
        else:
            pattern = "fund_flow_*.json"
            name_tmpl = DATA_FILENAME

        if date:
            # 指定日期：只加载该天文件，不存在则报错
            fp = DATA_DIR / name_tmpl.format(date=date)
            if not fp.exists():
                raise FileNotFoundError(f"数据文件不存在: {fp}")
            df = _read_jsonl(fp)
        else:
            default_fp = DATA_DIR / name_tmpl.format(date=today)
            fp = data_path or default_fp
            if not fp.exists():
                today_files = sorted(fp.parent.glob(pattern))
                if not today_files:
                    raise FileNotFoundError(f"数据文件不存在: {fp}")
                dfs = [_read_jsonl(f) for f in today_files]
                df = pd.concat(dfs, ignore_index=True)
            else:
                df = _read_jsonl(fp)
        df["time"] = pd.to_datetime(df["time"], format="%H:%M:%S")
        # 允许收盘后采集的数据（取 15:00 和实际最大时间的较大值）
        close_time = df["time"].iloc[0].replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0)
        max_time = df["time"].max()
        cutoff = max(close_time, max_time)
        df = df[df["time"] <= cutoff]
        # 显示层也过滤黑名单板块（兼容修复前采集的旧数据）
        blacklisted = {s for s in df["sector"].unique() if is_blacklisted(s)}
        if blacklisted:
            df = df[~df["sector"].isin(blacklisted)]
        return df

    def available_dates(self, source: str = "em") -> list[str]:
        """返回该数据源有数据文件的日期列表（降序）。"""
        pattern = "ths_fund_flow_*.json" if source == "ths" else "fund_flow_*.json"
        prefix = "ths_fund_flow_" if source == "ths" else "fund_flow_"
        dates = []
        files = list(DATA_DIR.glob(pattern))
        if source == "em":
            files += list((DATA_DIR / "history_daily").glob(pattern))
        for fp in files:
            date_str = fp.stem.replace(prefix, "")
            if len(date_str) == 8 and date_str.isdigit():
                dates.append(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}")
        return sorted(set(dates), reverse=True)

    def generate(self, df: pd.DataFrame, mode: str = "delta",
                 top_n: int = TOP_N, bottom_n: int = BOTTOM_N,
                 output: str = None, log_scale: bool = False,
                 title_extra: str = "", source: str = "em") -> go.Figure:

        source_label = "同花顺" if source == "ths" else "东方财富"
        value_col = "delta" if mode == "delta" else "main_net_inflow"
        y_label = "增量净流入（亿）" if mode == "delta" else "累计净流入（亿）"
        title_suffix = "· 增量" if mode == "delta" else "· 累计"
        if log_scale:
            title_suffix += " (对数)"
        if title_extra:
            title_suffix += f" · {title_extra}"

        last_tick = df["time"].max()
        min_records = df["time"].nunique() * 0.5  # 至少覆盖 50% 时间点
        sector_counts = df.groupby("sector").size()
        valid_sectors = sector_counts[sector_counts >= min_records].index

        last_slice = df[(df["time"] == last_tick) & (df["sector"].isin(valid_sectors))]
        ranking = last_slice.groupby("sector")["main_net_inflow"].last().sort_values(ascending=False)

        # 净流入前 N 名
        # 流入：只选正值板块，按净额降序
        positive_ranking = ranking[ranking > 0]
        top_sectors = positive_ranking.head(top_n).index.tolist()
        
        # 流出：只选负值板块，按最负的排序
        negative_ranking = ranking[ranking < 0].sort_values()
        bottom_sectors = negative_ranking.head(bottom_n).index.tolist() if bottom_n > 0 else []
        # 去重（防止板块极少时重叠）
        bottom_sectors = [s for s in bottom_sectors if s not in top_sectors]
        all_sectors = top_sectors + bottom_sectors

        df_top = df[df["sector"].isin(all_sectors)]

        # 始终显示 markers 保证 hover 可触发，数据多时用较小标记
        time_points = df["time"].nunique()
        marker_size = 6 if time_points <= 3 else 3

        fig = go.Figure()
        # 右侧标签规格：(板块, 期望y, 文本, 颜色, 字号) —— 收集后统一防重叠排布
        label_specs = []

        # --- 净流入板块（实线） ---
        for i, sector in enumerate(top_sectors):
            sub = df_top[df_top["sector"] == sector].sort_values("time")
            color = COLORS[i % len(COLORS)]
            fig.add_trace(go.Scatter(
                x=sub["time"],
                y=sub[value_col],
                mode="lines+markers",
                name=sector,
                line=dict(color=color, width=2),
                marker=dict(color=color, size=marker_size),
                hovertemplate=(
                    f"<b>{sector}</b><br>"
                    f"时间: %{{x|%H:%M:%S}}<br>"
                    f"{y_label}: %{{y:.2f}}亿<extra></extra>"
                ),
                hoverlabel=dict(namelength=-1, font_size=13),
            ))
            last_row = sub.iloc[-1]
            label_specs.append((
                sector, last_row[value_col],
                f"<b>{sector}</b> {fmt_value(last_row[value_col])}",
                color, 12,
            ))

        # --- 净流出板块（虚线，与净流入区分） ---
        for i, sector in enumerate(bottom_sectors):
            sub = df_top[df_top["sector"] == sector].sort_values("time")
            color = OUTFLOW_COLORS[i % len(OUTFLOW_COLORS)]
            fig.add_trace(go.Scatter(
                x=sub["time"],
                y=sub[value_col],
                mode="lines+markers",
                name=sector,
                line=dict(color=color, width=2, dash="dash"),
                marker=dict(color=color, size=marker_size, symbol="diamond"),
                hovertemplate=(
                    f"<b>{sector}</b><br>"
                    f"时间: %{{x|%H:%M:%S}}<br>"
                    f"{y_label}: %{{y:.2f}}亿<extra></extra>"
                ),
                hoverlabel=dict(namelength=-1, font_size=13),
            ))
            last_row = sub.iloc[-1]
            label_specs.append((
                sector, last_row[value_col],
                f"<b>{sector}</b> {fmt_value(last_row[value_col])}",
                color, 11,
            ))

        # 右侧标签防重叠：按收尾值贪心排布，避免名字/金额互相压盖
        y_vals = df_top[value_col].dropna()
        if len(y_vals):
            y_lo, y_hi = float(y_vals.min()), float(y_vals.max())
            pad = (y_hi - y_lo) * 0.05 or 1.0
            y_lo -= pad
            y_hi += pad
        else:
            y_lo, y_hi = 0.0, 1.0
        placed = _declutter_y([(s[0], s[1]) for s in label_specs], y_lo, y_hi)
        for sector, _desired_y, text, color, size in label_specs:
            fig.add_annotation(
                x=1.01,
                y=placed[sector],
                xref="paper",
                yref="y",
                text=text,
                showarrow=False,
                xanchor="left",
                yanchor="middle",
                font=dict(color=color, size=size, family="Microsoft YaHei, SimHei, sans-serif"),
                bgcolor="rgba(255,255,255,0.95)",
                bordercolor=color,
                borderwidth=1.5,
                borderpad=3,
            )

        fig.update_layout(
            title=dict(
                text=f"<b>{source_label} · 概念板块主力资金流向走势图 {title_suffix}</b>",
                font=dict(size=16, color="#333"),
                x=0.01,
                y=0.98,
                xanchor="left",
                yanchor="top",
            ),
            xaxis=dict(
                title="",
                type="date",
                tickformat="%H:%M",
                showgrid=False,
                zeroline=False,
                range=[
                    df_top["time"].iloc[0].replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0),
                    df_top["time"].iloc[0].replace(hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0),
                ],
                rangebreaks=[
                    dict(bounds=[11.5, 13], pattern="hour"),
                ],
                tickfont=dict(size=11, color="#666"),
            ),
            yaxis=dict(
                title=dict(text=y_label, font=dict(size=12, color="#666")),
                type="log" if log_scale else "linear",
                showgrid=True,
                gridcolor="#f0f0f0",
                zeroline=True,
                zerolinecolor="#ccc",
                zerolinewidth=1,
                tickfont=dict(size=11, color="#666"),
            ),
            plot_bgcolor="#fafafa",
            paper_bgcolor="white",
            hovermode="x unified",
            legend=dict(
                orientation="h",
                yanchor="top",
                y=0.99,
                xanchor="left",
                x=0.01,
                font=dict(size=10),
                bgcolor="rgba(255,255,255,0.8)",
                bordercolor="#ddd",
                borderwidth=1,
            ),
            margin=dict(l=60, r=220, t=80, b=40),
        )

        fig.add_annotation(
            x=0.995, y=0.01,
            xref="paper", yref="paper",
            text=f"更新于 {datetime.now().strftime('%H:%M:%S')}",
            showarrow=False,
            font=dict(size=10, color="#999"),
        )

        if output:
            plot_html = fig.to_html(include_plotlyjs=False, full_html=False, div_id="chart",
                                    config={"responsive": True, "displayModeBar": True})
            Path(output).write_text(_wrap_html(plot_html), encoding="utf-8")
            print(f"图表已保存: {output}")
        return fig

    def generate_json(self, df: pd.DataFrame, mode: str = "delta",
                      top_n: int = TOP_N, bottom_n: int = BOTTOM_N,
                      log_scale: bool = False, title_extra: str = "",
                      source: str = "em") -> dict:
        """生成图表数据的 JSON 表示，供前端 JS 动态渲染。"""

        source_label = "同花顺" if source == "ths" else "东方财富"
        value_col = "delta" if mode == "delta" else "main_net_inflow"
        y_label = "增量净流入（亿）" if mode == "delta" else "累计净流入（亿）"
        title_suffix = "· 增量" if mode == "delta" else "· 累计"
        if log_scale:
            title_suffix += " (对数)"
        if title_extra:
            title_suffix += f" · {title_extra}"

        last_tick = df["time"].max()
        min_records = df["time"].nunique() * 0.5
        sector_counts = df.groupby("sector").size()
        valid_sectors = sector_counts[sector_counts >= min_records].index

        last_slice = df[(df["time"] == last_tick) & (df["sector"].isin(valid_sectors))]
        ranking = last_slice.groupby("sector")["main_net_inflow"].last().sort_values(ascending=False)

        # 流入：只选正值板块，按净额降序
        positive_ranking = ranking[ranking > 0]
        top_sectors = positive_ranking.head(top_n).index.tolist()
        
        # 流出：只选负值板块，按最负的排序
        negative_ranking = ranking[ranking < 0].sort_values()
        bottom_sectors = negative_ranking.head(bottom_n).index.tolist() if bottom_n > 0 else []
        all_sectors = top_sectors + bottom_sectors

        df_top = df[df["sector"].isin(all_sectors)]
        time_points = df["time"].nunique()

        x_range_start = df_top["time"].iloc[0].replace(
            hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0
        ).strftime("%Y-%m-%d %H:%M:%S")
        x_range_end = df_top["time"].iloc[0].replace(
            hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute, second=0
        ).strftime("%Y-%m-%d %H:%M:%S")

        sectors_data = []

        # 净流入板块
        for i, sector in enumerate(top_sectors):
            sub = df_top[df_top["sector"] == sector].sort_values("time")
            sectors_data.append({
                "name": sector,
                "times": sub["time"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
                "values": sub[value_col].round(4).tolist(),
                "color": COLORS[i % len(COLORS)],
                "is_outflow": False,
                "y_label": y_label,
            })

        # 净流出板块
        for i, sector in enumerate(bottom_sectors):
            sub = df_top[df_top["sector"] == sector].sort_values("time")
            sectors_data.append({
                "name": sector,
                "times": sub["time"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
                "values": sub[value_col].round(4).tolist(),
                "color": OUTFLOW_COLORS[i % len(OUTFLOW_COLORS)],
                "is_outflow": True,
                "y_label": y_label,
            })

        return {
            "source_label": source_label,
            "y_label": y_label,
            "title_suffix": title_suffix,
            "log_scale": log_scale,
            "time_points": time_points,
            "x_range": [x_range_start, x_range_end],
            "updated_at": datetime.now().strftime("%H:%M:%S"),
            "sectors": sectors_data,
        }

    def show(self, df: pd.DataFrame, mode: str = "delta", top_n: int = TOP_N,
             bottom_n: int = BOTTOM_N, log_scale: bool = False):
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        html_path = CHART_DIR / "fund_flow.html"
        fig = self.generate(df, mode=mode, top_n=top_n, bottom_n=bottom_n, log_scale=log_scale)
        plot_html = fig.to_html(include_plotlyjs=False, full_html=False, div_id="chart",
                                config={"responsive": True, "displayModeBar": True})
        html_path.write_text(_wrap_html(plot_html), encoding="utf-8")
        print(f"图表已保存: {html_path}")
        webbrowser.open(f"file://{html_path}")

    def show_dual(self, em_df: pd.DataFrame, ths_df: pd.DataFrame,
                  mode: str = "delta", top_n: int = TOP_N, bottom_n: int = BOTTOM_N,
                  log_scale: bool = False):
        CHART_DIR.mkdir(parents=True, exist_ok=True)
        html_path = CHART_DIR / "fund_flow.html"
        html = self._build_dual_html(em_df, ths_df, mode=mode, top_n=top_n,
                                     bottom_n=bottom_n, log_scale=log_scale)
        html_path.write_text(html, encoding="utf-8")
        print(f"双 Tab 图表已保存: {html_path}")
        webbrowser.open(f"file://{html_path}")

    def _build_dual_html(self, em_df: pd.DataFrame = None, ths_df: pd.DataFrame = None,
                         mode: str = "delta", top_n: int = TOP_N, bottom_n: int = BOTTOM_N,
                         log_scale: bool = False,
                         refresh_seconds: int = None, title_extra: str = "") -> str:
        plot_config = {"responsive": True, "displayModeBar": True}

        em_chart = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#999;">暂无东方财富数据</div>'
        ths_chart = '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:#999;">暂无同花顺数据</div>'

        if em_df is not None and len(em_df) > 0:
            em_fig = self.generate(em_df, mode=mode, top_n=top_n, bottom_n=bottom_n,
                                   log_scale=log_scale, source="em", title_extra=title_extra)
            em_chart = em_fig.to_html(include_plotlyjs=False, full_html=False,
                                      div_id="chart-em", config=plot_config)

        if ths_df is not None and len(ths_df) > 0:
            ths_fig = self.generate(ths_df, mode=mode, top_n=top_n, bottom_n=bottom_n,
                                    log_scale=log_scale, source="ths", title_extra=title_extra)
            ths_chart = ths_fig.to_html(include_plotlyjs=False, full_html=False,
                                        div_id="chart-ths", config=plot_config)

        return _wrap_dual_html(em_chart, ths_chart, refresh_seconds=refresh_seconds)


def period_data(period="today", date=None):
    if period not in ("today", "week", "month", "year"):
        raise ValueError("invalid period")
    today = datetime.now().date()
    end = datetime.strptime(date, "%Y%m%d").date() if date else today
    start = end if period == "today" else (end - timedelta(days=end.weekday()) if period == "week" else end.replace(day=1) if period == "month" else end.replace(month=1, day=1))
    days = [start + timedelta(days=i) for i in range((end-start).days+1)]
    frames, available, stamps = [], [], {}
    for day in days:
        fp = DATA_DIR / DATA_FILENAME.format(date=day.strftime("%Y%m%d"))
        hist_fp = DATA_DIR / "history_daily" / DATA_FILENAME.format(date=day.strftime("%Y%m%d"))
        source_frames = []
        if fp.exists():
            source_frames.append(_read_jsonl(fp))
        if hist_fp.exists() and day < today:
            source_frames.append(_read_jsonl(hist_fp))
        if not source_frames:
            continue
        df = pd.concat(source_frames, ignore_index=True)
        if df.empty:
            continue
        df = df[~df["sector"].map(is_blacklisted)].copy()
        df["time"] = pd.to_datetime(day.isoformat()+" "+df["time"].astype(str), errors="coerce")
        df["main_net_inflow"] = pd.to_numeric(df["main_net_inflow"], errors="coerce")
        df = df.dropna(subset=["time", "main_net_inflow"])
        if df.empty:
            continue
        available.append(day.isoformat())
        stamps[day.isoformat()] = df["time"].max().strftime("%H:%M:%S")
        if period != "today":
            # Last observed daily cumulative value per board, never sum intraday snapshots.
            df = df.sort_values("time").groupby("sector", as_index=False).tail(1)
        frames.append(df)
    missing = [d.isoformat() for d in days if d.weekday()<5 and d.isoformat() not in available]
    note = ("今天/单日：盘中当日累计净额，5分钟采样。" if period == "today" else "周/月/年：每日最后观测的当日净额，不是跨日累计，不相加日内快照。")
    note += " 范围 "+start.isoformat()+"～"+end.isoformat()+"；有记录 "+str(len(available))+" 天。"
    note += " 历史覆盖以本地实际文件为准，缺失仍留空（不补零）。历史日终来自东方财富板块资金流接口，15:00为日终绘图时间标记，不是盘中采样；实时仍为5分钟采样。未采集日期不补零、不造历史；工作日无记录 "+str(len(missing))+" 天（含未开盘/节假日，未接入交易日历）。"
    if available:
        note += " 实际覆盖 "+available[0]+"～"+available[-1]+"；末次观测 "+available[-1]+" "+stamps[available[-1]]+"。历史使用源站日终净额；当天交易中仅截至最新采样，不是收盘值。"
    result = {"period":period, "start":start.isoformat(), "end":end.isoformat(), "available_dates":available, "missing_weekdays":missing, "note":note, "em":None}
    if not frames:
        return result
    df = pd.concat(frames, ignore_index=True)
    lastday = available[-1]
    latest = df[df["time"].dt.strftime("%Y-%m-%d")==lastday].sort_values("time").groupby("sector").tail(1)
    ranking = latest.set_index("sector")["main_net_inflow"].sort_values(ascending=False)
    chosen = [(n,False) for n in ranking[ranking>0].head(30).index] + [(n,True) for n in ranking[ranking<0].sort_values().head(15).index]
    sectors=[]
    for i,(name,out) in enumerate(chosen):
        sub=df[df["sector"]==name].sort_values("time")
        if period == "today":
            times=sub["time"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist()
            values=sub["main_net_inflow"].round(4).tolist()
        else:
            daily={t.strftime("%Y-%m-%d"):round(float(v),4) for t,v in zip(sub["time"],sub["main_net_inflow"])}
            observed={t.strftime("%Y-%m-%d"):t.strftime("%Y-%m-%d %H:%M:%S") for t in sub["time"]}
            times=[observed.get(d.isoformat(), d.isoformat()+" 12:00:00") for d in days]
            values=[daily.get(d.isoformat()) for d in days]
        sectors.append({"name":name,"times":times,"values":values,"color":(OUTFLOW_COLORS if out else COLORS)[i%len(OUTFLOW_COLORS if out else COLORS)],"is_outflow":out,"y_label":"当日净额（亿元）"})
    result["em"]={"source_label":"东方财富","y_label":"当日净额（亿元）","title_suffix":"· "+{"today":"单日盘中","week":"本周·每日末次净额","month":"本月·每日末次净额","year":"本年·每日末次净额"}[period],"log_scale":False,"time_points":df["time"].nunique(),"period_view":period!="today","x_range":[start.isoformat()+(" 09:30:00" if period=="today" else " 00:00:00"),end.isoformat()+(" 15:00:00" if period=="today" else " 23:59:59")],"updated_at":available[-1]+" "+stamps[available[-1]],"sectors":sectors}
    return result

class _ChartHandler(http.server.SimpleHTTPRequestHandler):
    data_gen = None       # 返回 dict 的回调，签名 data_gen(date=None)
    dates_gen = None      # 返回可用日期列表的回调
    ajax_html = ""        # AJAX 模板 HTML
    refresh_seconds = 60
    _cache = {}           # {date_str: {"json":..., "mtime":...}}
    _cache_lock = threading.Lock()

    def _authorized(self):
        import base64, hashlib, hmac
        auth = json.loads(Path("/app/auth.json").read_text())
        if self.path == "/api/health" and hmac.compare_digest(self.headers.get("X-Health-Token", ""), auth["health_token"]):
            return True
        try:
            scheme, encoded = self.headers.get("Authorization", "").split(" ", 1)
            if scheme.lower() != "basic":
                raise ValueError()
            user, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(auth["salt"]), 200000).hex()
            if hmac.compare_digest(user, auth["username"]) and hmac.compare_digest(digest, auth["password_hash"]):
                return True
        except (ValueError, UnicodeError):
            pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="A-Flow Radar", charset="UTF-8"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_HEAD(self):
        if not self._authorized():
            return
        self.send_error(405)

    def do_GET(self):
        if not self._authorized():
            return
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            # 返回 AJAX 模板（静态 HTML，数据由 JS 轮询获取）
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(self.ajax_html.encode("utf-8"))

        elif path == "/api/data":
            # date 参数：YYYY-MM-DD 或 YYYYMMDD，缺省为当日
            date = self._normalize_date(query.get("date", [None])[0])
            try:
                data = period_data(query.get('period', ['today'])[0], date)
                body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))

        elif path == "/api/dates":
            try:
                dates = self.dates_gen()
                body = json.dumps(dates, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))

        elif path == "/static/plotly.min.js":
            body = Path("/app/static/plotly.min.js").read_bytes()
            self.send_response(200)
            self.send_header("Content-type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/health":
            mtime = self._data_mtime(datetime.now().strftime("%Y%m%d"))
            age = time.time() - mtime if mtime else None
            healthy = bool(mtime) and (not is_collecting_hours() or age < 660)
            self.send_response(200 if healthy else 503)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok" if healthy else "stale_or_missing", "last_collection": datetime.fromtimestamp(mtime).isoformat() if mtime else None, "age_seconds": round(age, 1) if age else None, "interval_seconds": 300, "session": market_session(), "source": "eastmoney", "note": "collection time, not provider timestamp; weekday schedule does not include holiday calendar"}).encode())
        else:
            self.send_error(404)

    @staticmethod
    def _normalize_date(date_str):
        """将 YYYY-MM-DD 转为 YYYYMMDD；None/空 返回 None（当日）。"""
        if not date_str:
            return None
        value = date_str.replace("-", "")
        if not re.fullmatch(r"\d{8}", value):
            raise ValueError("invalid date")
        datetime.strptime(value, "%Y%m%d")
        return value

    def _get_cached_data(self, date):
        date_key = date or datetime.now().strftime("%Y%m%d")
        mtime = self._data_mtime(date_key)
        if mtime == 0:
            raise FileNotFoundError("暂无数据")
        with self._cache_lock:
            entry = self._cache.get(date_key)
            if entry and entry["mtime"] == mtime:
                return entry["json"]
        data = self.data_gen(date)
        with self._cache_lock:
            self._cache[date_key] = {"json": data, "mtime": mtime}
        return data

    @staticmethod
    def _data_mtime(date_str):
        em_fp = DATA_DIR / DATA_FILENAME.format(date=date_str)
        ths_fp = DATA_DIR / THS_DATA_FILENAME.format(date=date_str)
        return max(
            em_fp.stat().st_mtime if em_fp.exists() else 0,
            ths_fp.stat().st_mtime if ths_fp.exists() else 0,
        )

    def log_message(self, format, *args):
        pass  # 静默日志，减少干扰


def _make_handler(data_gen, dates_gen, ajax_html, refresh_seconds=60):
    cls = type("Handler", (_ChartHandler,), {
        "data_gen": staticmethod(data_gen),
        "dates_gen": staticmethod(dates_gen),
        "ajax_html": ajax_html,
        "refresh_seconds": refresh_seconds,
    })
    return cls


def _build_ajax_html(refresh_seconds: int = 60) -> str:
    """生成 AJAX 模板 HTML，注入配置参数。"""
    return (
        AJAX_TEMPLATE
        .replace("__POLL_MS__", str(refresh_seconds * 1000))
        .replace("__COLORS_JSON__", json.dumps(COLORS))
        .replace("__OUTFLOW_JSON__", json.dumps(OUTFLOW_COLORS))
    )


def _start_http_server(data_gen, dates_gen, port: int = HTTP_PORT, refresh_seconds: int = 60):
    ajax_html = _build_ajax_html(refresh_seconds)
    handler = _make_handler(data_gen, dates_gen, ajax_html, refresh_seconds)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    with socketserver.ThreadingTCPServer(("", port), handler) as httpd:
        print(f"图表服务: http://localhost:{port}")
        httpd.serve_forever()


def _load_data(chart: FundFlowChart, source: str = "em", date: str = None):
    """加载数据，不存在则返回 None。date 格式 YYYYMMDD，None 表示当日/最新。"""
    try:
        return chart.load_data(source=source, date=date)
    except FileNotFoundError:
        return None


def main():
    parser = argparse.ArgumentParser(description="资金流向实时走势图")
    parser.add_argument("--once", action="store_true", help="单次采集并生成图表")
    parser.add_argument("--collect", action="store_true", help="仅后台采集数据")
    parser.add_argument("--chart", action="store_true", help="仅从已有数据生成图表")
    parser.add_argument("--cleanup", action="store_true", help="仅清理过期数据文件")
    parser.add_argument("--csv", type=str, help="从 CSV 文件加载数据生成图表")
    parser.add_argument("--live", action="store_true", help="（默认行为）采集 + HTTP 实时图表")
    parser.add_argument("--port", type=int, default=HTTP_PORT, help=f"HTTP 端口 (默认 {HTTP_PORT})")
    parser.add_argument("--interval", type=int, default=COLLECT_INTERVAL,
                        help=f"采集间隔秒数 (默认 {COLLECT_INTERVAL})")
    parser.add_argument("--keep-days", type=int, default=30,
                        help="数据保留天数 (默认 30)")
    parser.add_argument("--top", type=int, default=TOP_N, help=f"显示净流入板块数 (默认 {TOP_N})")
    parser.add_argument("--bottom", type=int, default=BOTTOM_N, help=f"显示净流出板块数 (默认 {BOTTOM_N})")
    parser.add_argument("--mode", choices=["delta", "cumulative"], default="cumulative",
                        help="显示模式: cumulative=累计 (默认), delta=增量")
    parser.add_argument("--log", action="store_true", help="Y 轴使用对数坐标")
    parser.add_argument("--timeout", type=int, default=0,
                        help="采集 N 秒后自动退出 (0=手动 Ctrl+C)")
    args = parser.parse_args()

    collector = FundFlowCollector()
    ths_collector = THSFundFlowCollector() if HAS_THS_AUTH else None

    if args.cleanup:
        collector._cleanup(keep_days=args.keep_days)
        if ths_collector:
            ths_collector._cleanup(keep_days=args.keep_days)
        return

    if args.once:
        print("单次采集...")
        collector.snapshot()
        if ths_collector:
            ths_collector.snapshot()

        chart = FundFlowChart()
        em_df = _load_data(chart, source="em")
        ths_df = _load_data(chart, source="ths")

        if em_df is not None and ths_df is not None:
            chart.show_dual(em_df, ths_df, mode=args.mode, top_n=args.top,
                            bottom_n=args.bottom, log_scale=args.log)
        elif em_df is not None:
            chart.show(em_df, mode=args.mode, top_n=args.top,
                       bottom_n=args.bottom, log_scale=args.log)
        else:
            print("无数据可显示")
        return

    if args.collect:
        print("后台采集模式，按 Ctrl+C 停止")
        collector.start(interval=args.interval)
        if ths_collector:
            ths_collector.start(interval=args.interval)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            collector.stop()
            if ths_collector:
                ths_collector.stop()
        return

    if args.chart:
        chart = FundFlowChart()
        try:
            data_path = Path(args.csv) if args.csv else None
            em_df = chart.load_data(data_path, source="em") if not args.csv else chart.load_data(data_path)
            ths_df = _load_data(chart, source="ths") if not args.csv else None
            if ths_df is not None:
                chart.show_dual(em_df, ths_df, mode=args.mode, top_n=args.top,
                                bottom_n=args.bottom, log_scale=args.log)
            else:
                chart.show(em_df, mode=args.mode, top_n=args.top,
                           bottom_n=args.bottom, log_scale=args.log)
        except FileNotFoundError as e:
            print(e)
            sys.exit(1)
        return

    # 默认模式：实时采集 + HTTP 服务
    collector.start(interval=args.interval)
    if ths_collector:
        ths_collector.start(interval=args.interval)
    chart = FundFlowChart()

    print("=" * 60)
    print("  资金流向实时走势图（东方财富 + 同花顺）")
    print(f"  累计模式 · 净流入前 {args.top} + 净流出前 {args.bottom} · {args.interval}s 采集")
    print(f"  图表服务: http://localhost:{args.port}")
    print("  Ctrl+C 停止")
    print("=" * 60)

    def gen_data(date=None):
        session = market_session()
        title_extra = {
            "lunch": "上午半场总结",
            "closed": "当日总结",
        }.get(session, "")
        em_df = _load_data(chart, source="em", date=date)
        ths_df = _load_data(chart, source="ths", date=date)
        if em_df is None and ths_df is None:
            raise FileNotFoundError("暂无数据")
        result = {}
        if em_df is not None:
            result["em"] = chart.generate_json(em_df, mode=args.mode, top_n=args.top,
                                               bottom_n=args.bottom, log_scale=args.log,
                                               title_extra=title_extra, source="em")
        if ths_df is not None:
            result["ths"] = chart.generate_json(ths_df, mode=args.mode, top_n=args.top,
                                                bottom_n=args.bottom, log_scale=args.log,
                                                title_extra=title_extra, source="ths")
        return result

    def gen_dates():
        em_dates = chart.available_dates(source="em")
        ths_dates = chart.available_dates(source="ths")
        return {"em": em_dates, "ths": ths_dates}

    try:
        _start_http_server(gen_data, gen_dates, port=args.port, refresh_seconds=args.interval)
    except KeyboardInterrupt:
        collector.stop()
        if ths_collector:
            ths_collector.stop()
    print("\n已退出")


if __name__ == "__main__":
    main()