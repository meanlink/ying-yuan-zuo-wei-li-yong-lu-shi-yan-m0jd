import {request} from './api.js';
import {escape} from './operations.js';

const status = order => order.excluded
  ? `<span class="ops-status warn">整单剔除 · 跨组</span>`
  : `<span class="ops-status ok">入样</span>`;

function entryRows(order) {
  return order.entries.map(entry => `<li><b>${escape(entry.hall_name)} · ${escape(entry.seat_zone)}</b>
    <span class="${escape(entry.group_name)}">${escape(entry.group_name)}</span> · ${entry.seats} 座
    <small>${escape(entry.session_code)}${entry.note ? ` · ${escape(entry.note)}` : ''}</small></li>`).join('');
}

function orderCard(order) {
  const groups = Object.entries(order.groups).map(([group, seats]) => `${escape(group)} ${seats}`).join(' / ');
  return `<article class="ops-trial"><div class="ops-trial-head"><div><h4>订单 ${escape(order.order_no)}</h4>
    <p>${order.seat_count} 座 · ${groups || '尚未补录'}</p></div>${status(order)}</div>
    <p>${order.excluded ? '座位横跨两组，整单退出实验样本；票房统计仍照常计入。' : '座位处于同一实验组，纳入实验样本。'}</p>
    ${order.entries.length ? `<details class="ops-history" open><summary>补录明细 · ${order.entries.length} 次</summary><ul>${entryRows(order)}</ul></details>` : ''}
  </article>`;
}

export function groupbuyPage(state) {
  const plan = state.selectedPlan;
  const orders = state.groupOrders || [];
  const units = state.units || [];
  const canRecord = ['boxoffice','admin'].includes(state.user.role) && ['live','review','approved'].includes(plan?.status);
  const excluded = orders.filter(order => order.excluded);
  const form = canRecord ? `<form class="ops-form" data-groupbuy="entry">
    <label>团购订单号<input name="order_no" minlength="1" maxlength="40" placeholder="GB-20261121-001" required></label>
    <label>入座实验单元<select name="unit_id" required>${units.map(unit => `<option value="${unit.id}">${escape(unit.hall_name)} · ${escape(unit.seat_zone)} · ${escape(unit.group_name)}</option>`).join('')}</select></label>
    <label>本次补录座位数<input name="seats" type="number" min="1" max="500" required></label>
    <label>补录说明<input name="note" maxlength="600" placeholder="分次补录来源或批次说明"></label>
    <button class="button primary" type="submit">登记团购票</button>
  </form>` : '';
  const list = orders.length ? orders.map(orderCard).join('') : '<p>暂无团购订单登记。</p>';
  return `<p class="ops-intro">团购票按订单号登记，可重复提交同一订单号分次补录。所有座位照常进入票房统计；订单一旦横跨两组，整单退出实验样本，引用过它的复盘会失效并需重算再审。</p>
    <div class="ops-layout">${panel(`团购订单 · ${orders.length} 单`, list)}
    ${panel('登记 / 补录', form + `<div class="ops-warning">已被剔除 ${excluded.length} 单 · ${excluded.reduce((sum, order) => sum + order.seat_count, 0)} 座</div>`)}</div>`;
}

function panel(title, body) {
  return `<section class="panel"><div class="panel-head"><h3>${title}</h3></div>${body}</section>`;
}

export async function loadGroupbuy(state) {
  if (state.current !== 'groupbuy') return;
  const id = state.selectedPlan?.id;
  state.groupOrders = id ? (await request(`/plans/${id}/group-orders`)).items : [];
}

export function bindGroupbuy(state, render, run, notify) {
  const form = document.querySelector('form[data-groupbuy="entry"]');
  if (!form) return;
  form.onsubmit = event => {
    event.preventDefault();
    const fields = Object.fromEntries(new FormData(form));
    fields.unit_id = Number(fields.unit_id);
    fields.seats = Number(fields.seats);
    const submit = form.querySelector('button[type=submit]');
    submit.disabled = true;
    run(async () => {
      try {
        const result = await request(`/plans/${state.selectedPlan.id}/group-orders`, {
          method: 'POST', body: JSON.stringify(fields),
        });
        await loadGroupbuy(state);
        render();
        const invalidated = result.invalidated_reviews?.length || 0;
        notify(result.excluded
          ? `订单横跨两组，整单退出实验样本${invalidated ? `，${invalidated} 份复盘已失效` : ''}。`
          : '团购票已入账。');
      } finally {
        submit.disabled = false;
      }
    });
  };
}

export function groupReviewSummary(review) {
  const detail = review.group_orders;
  if (!detail) return '';
  const excluded = detail.excluded_group_orders || [];
  const list = excluded.length
    ? `<ul class="group-excluded">${excluded.map(order => `<li><b>${escape(order.order_no)}</b> · ${order.seats} 座 · ${escape(Object.keys(order.groups || {}).join(' / '))}</li>`).join('')}</ul>`
    : '<p class="small-note">本次复盘没有剔除的团购订单。</p>';
  const invalidated = detail.invalidated_reason
    ? `<p class="ops-warning">该复盘已失效：${escape(detail.invalidated_reason)}。请重算后再审。</p>` : '';
  return `<div class="group-review"><span class="eyebrow">GROUP ORDERS</span>
    <p>入样团购 ${detail.included_group_orders?.length || 0} 单 · ${detail.included_group_seats || 0} 座；
       剔除 ${excluded.length} 单 · ${detail.excluded_group_seats || 0} 座。</p>${list}${invalidated}</div>`;
}

export function groupReviewHistory(reviews) {
  if (!reviews?.length) return '';
  const rows = reviews.map(review => {
    const detail = review.group_orders || {};
    const excluded = detail.excluded_group_orders || [];
    const orders = excluded.length
      ? excluded.map(order => `${escape(order.order_no)}（${order.seats} 座）`).join('、')
      : '无';
    return `<tr><td>#${review.id}</td><td><span class="status ${review.analysis_status}">${review.analysis_status}</span></td>
      <td>${review.sample_size}</td><td>${excluded.length} 单 / ${detail.excluded_group_seats || 0} 座</td>
      <td>${orders}</td></tr>`;
  }).join('');
  return `<section class="panel"><div class="panel-head"><div><span class="eyebrow">REVIEW HISTORY</span><h3>复盘历史与剔除团购</h3></div></div>
    <table><thead><tr><th>复盘</th><th>状态</th><th>样本</th><th>剔除汇总</th><th>剔除订单</th></tr></thead><tbody>${rows}</tbody></table></section>`;
}
