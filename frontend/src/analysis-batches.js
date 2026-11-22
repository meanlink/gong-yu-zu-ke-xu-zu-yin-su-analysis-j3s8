import {api} from './api.js';
import {el, table, toast, badge} from './components.js';

function fmtTime(value){
  if (!value) return '—';
  return new Date(value).toLocaleString('zh-CN', {hour12: false});
}

// 复核预览：严格按所选批次的快照展示，派单与跳过（含原因）分开列
function previewTable(preview, readonly){
  const rows = preview.map(item => {
    const status = item.action === 'dispatch'
      ? badge(item.priority === 'high' ? '高优先级派单' : '常规派单', item.priority === 'high' ? 'red' : 'orange')
      : badge('跳过', 'neutral');
    return [
      item.tenant_name,
      item.lease_period,
      `${item.renewal_intent}/10`,
      item.service_score,
      item.lease_end_snapshot,
      status.textContent,
      item.action === 'dispatch' ? item.reason : item.skip_reason,
    ];
  });
  return table(['租客', '租期', '意愿', '服务', '快照租期止', '动作', '依据 / 跳过原因'], rows, '该批次没有样本');
}

// 分析留档：每批成功分析的样本、计算结果和跟进预览都可回看
async function batchHistory(main){
  const batches = await api('/analysis/batches');
  const detailWrap = el('div');
  const rows = batches.map(b => {
    const openBtn = el('button', {class: 'button tiny', text: '查看留档', onClick: async () => {
      detailWrap.innerHTML = '';
      const detail = await api(`/analysis/batches/${b.id}`);
      detailWrap.append(batchDetailPanel(detail));
      window.scrollTo({top: detailWrap.offsetTop - 20, behavior: 'smooth'});
    }});
    return [
      `#${b.id}`,
      fmtTime(b.created_at),
      b.created_by_name || '—',
      b.sample_size,
      `${b.dispatch_count} 派单 / ${b.skip_count} 跳过`,
      b.reviewed_at ? `已复核 · ${fmtTime(b.reviewed_at)}` : '待复核',
      openBtn,
    ];
  });
  main.append(el('section', {class: 'panel'}, [
    el('div', {class: 'panel-title'}, [
      el('h2', {text: '分析批次留档'}),
      badge(`${batches.length} 批`, 'blue'),
    ]),
    table(['批次', '生成时间', '分析人', '样本数', '跟进预览', '复核状态', '留档'], rows, '尚无分析批次'),
    el('p', {class: 'muted', text: '每批成功分析都留档样本数据、计算结果和跟进预览。复核只按批次快照派单，新批次发布不影响历史批次查看。'}),
  ]));
  main.append(detailWrap);
}

function batchDetailPanel(detail){
  const a = detail.analysis_result;
  return el('section', {class: 'panel'}, [
    el('div', {class: 'panel-title'}, [
      el('h2', {text: `批次 #${detail.id} 留档明细`}),
      detail.is_latest ? badge('最新批次', 'green') : badge('历史批次', 'neutral'),
    ]),
    el('div', {class: 'model-strip'}, [
      el('span', {text: `样本 ${detail.sample_size} 份（快照）`}),
      el('span', {text: `偏相关 ${a.partial_correlation}`}),
      el('span', {text: `R² ${a.stepwise.r2}`}),
      el('span', {text: `入模 ${a.stepwise.selected_features.join(' · ')}`}),
      el('span', {text: `生成于 ${fmtTime(detail.created_at)}`}),
    ]),
    el('p', {class: 'muted', text: a.explanation}),
    el('h3', {text: '跟进预览（复核按此派单）'}),
    previewTable(detail.followup_preview, true),
  ]);
}

// 复核工作台：加载最新批次 -> 按预览复核 -> 处理并发替换与幂等重试
export async function renderReviewWorkspace(main, user, rerender){
  let batch;
  try {
    batch = await api('/reviews/latest-preview');
  } catch (err) {
    main.append(el('section', {class: 'panel'}, [
      el('div', {class: 'panel-title'}, [el('h2', {text: '客户经营复核'})]),
      el('p', {class: 'muted', text: err.message}),
    ]));
    await batchHistory(main);
    return;
  }

  const dispatchCount = batch.followup_preview.filter(i => i.action === 'dispatch').length;
  const skipCount = batch.followup_preview.length - dispatchCount;
  const reviewed = !!batch.reviewed_at;

  main.append(el('section', {class: 'panel'}, [
    el('div', {class: 'panel-title'}, [
      el('h2', {text: `客户经营复核 · 批次 #${batch.id}`}),
      reviewed ? badge('已复核（重试返回原结果）', 'green') : badge(batch.is_latest ? '最新批次' : '已被新批次替换', batch.is_latest ? 'green' : 'red'),
    ]),
    el('div', {class: 'model-strip'}, [
      el('span', {text: `快照样本 ${batch.sample_size} 份`}),
      el('span', {text: `偏相关 ${batch.partial_correlation}`}),
      el('span', {text: `R² ${batch.stepwise.r2}`}),
      el('span', {text: `预览派单 ${dispatchCount} · 跳过 ${skipCount}`}),
      el('span', {text: `生成于 ${fmtTime(batch.created_at)}`}),
    ]),
    el('p', {class: 'muted', text: '复核依据该批次留档的样本快照，补录问卷或报修结修不会改变本批数据。已退租或租期变化的租客在提交时再次核对并列出跳过原因。'}),
    previewTable(batch.followup_preview, true),
  ]));

  if (user.role === 'customer_manager') {
    const noteInput = el('input', {name: 'note', value: reviewed ? (batch.review_note || '已复核模型解释与租客原话，安排分级沟通') : '已复核模型解释与租客原话，安排分级沟通', required: true});
    const approveBtn = el('button', {class: 'button orange', text: reviewed ? '重试复核（幂等，不重复派单）' : '按预览派单并完成复核'});
    const bar = el('section', {class: 'action-bar'}, [
      el('div', {}, [
        el('strong', {text: '复核动作'}),
        el('p', {class: 'muted', text: '整批在一个事务内提交：中途失败不留半批跟进；重试不重复派单，已办成的复核返回原结果。'}),
      ]),
      el('form', {id: 'batch-review-form', class: 'action-row'}, [
        el('label', {class: 'compact'}, ['复核备注', noteInput]),
        approveBtn,
      ]),
    ]);
    main.append(bar);
    document.querySelector('#batch-review-form').addEventListener('submit', async e => {
      e.preventDefault();
      approveBtn.disabled = true;
      try {
        const r = await api('/reviews/approve', {
          method: 'POST',
          body: JSON.stringify({batch_id: batch.id, note: noteInput.value || '已按批次预览完成复核'}),
        });
        if (r.idempotent_replay) {
          toast(`该批次已复核，返回原结果：跟进 ${r.created_followups.length} 条，未重复派单`, 'success');
        } else {
          toast(`复核完成：派单 ${r.created_followups.length} 条，跳过 ${r.skipped.length} 条`, 'success');
        }
        rerender();
      } catch (err) {
        approveBtn.disabled = false;
        // 新批次发布与复核并发：旧批被替换，提示重新打开
        if (err.message.includes('替换') || err.message.includes('重新打开')) {
          toast(`${err.message}`, 'error');
          rerender();
        } else {
          toast(err.message, 'error');
        }
      }
    });
  }

  await batchHistory(main);
}
