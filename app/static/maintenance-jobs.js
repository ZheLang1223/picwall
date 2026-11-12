/* 维护任务只登记一次；每次查询有超时，刷新页面可继续观察后台任务。 */
export function maintenanceResult(kind, result = {}) {
  if (kind === 'media') return `已补齐 ${result.saved || 0} 张图，剩余 ${result.remaining || 0} 帖待补抓`;
  const history = result.history || {};
  const current = result.current_complete ? '当前保留图片已归档'
    : `归档检查完成，仍有 ${history.missing || result.failed_count || 0} 张保留图片未归档${history.missing_originals ? '，请先补抓缺失原图' : ''}`;
  const issues = [];
  if (history.historical_missing) issues.push(`${history.historical_missing} 条缺失`);
  if (history.historical_corrupt) issues.push(`${history.historical_corrupt} 条校验失败`);
  if (history.historical_unchecked) issues.push(`${history.historical_unchecked} 条尚未校验`);
  return current + (issues.length ? '；历史归档：' + issues.join('、') : '');
}

export function createMaintenanceJobs({api, toast, onJob, onComplete,
                                      pause = ms => new Promise(resolve => setTimeout(resolve, ms))}) {
  const watching = new Map();
  const active = job => ['queued','running'].includes(job.state);
  function observe(job) {
    if (!job?.id || !active(job) || watching.get(job.kind) === job.id) return;
    watching.set(job.kind, job.id);
    const watch = async () => {
      let failures = 0;
      while (watching.get(job.kind) === job.id) {
        try {
          const current = (await api('/api/jobs/' + job.kind)).job;
          if (current.id !== job.id) {
            watching.delete(job.kind); onJob(current); observe(current); return;
          }
          onJob(current); failures = 0;
          if (!active(current)) {
            if (current.state === 'complete') toast(maintenanceResult(job.kind, current.result));
            else toast(current.error || '任务未完成，可重试', undefined, {persistent:true});
            try { await onComplete(job.kind); }
            catch(e) { toast('任务结果已保存，页面刷新失败；可刷新页面核对', undefined, {persistent:true}); }
            return;
          }
        } catch(e) {
          if (++failures >= 3) {
            toast('任务状态刷新失败，后台任务可能仍在进行；刷新页面核对',
                  {label:'刷新核对',fn:() => location.reload()}, {persistent:true});
            return;
          }
        }
        await pause(2000);
      }
    };
    watch().finally(() => { if (watching.get(job.kind) === job.id) watching.delete(job.kind); });
  }
  async function start(kind) {
    try {
      const {job} = await api('/api/' + kind + '/retry','POST');
      onJob(job);
      toast(kind === 'archive' ? '归档检查正在进行，刷新页面后仍可查看进度' : '图片补抓正在进行，刷新页面后仍可查看进度');
      observe(job);
    } catch(e) { toast(e.message, undefined, {persistent:true}); }
  }
  return {start, observe};
}
