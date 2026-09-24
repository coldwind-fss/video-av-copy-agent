(() => {
  const APP_BASE_PATH = document.querySelector('meta[name="app-base-path"]')?.content || '';
  const APP_READ_ONLY = document.querySelector('meta[name="app-read-only"]')?.content === 'true';
  const appPath = (path) => `${APP_BASE_PATH}${path}`;
  const $ = (selector) => document.querySelector(selector);
  const state = { video: null, comparisonId: null, polling: null, lastComparison: null, method: location.hash === '#method=B' ? 'B' : 'A', contentVideo: null, transcriptId: null, transcriptionJobId: null, analysisId: null, relatedContentId: null, contentPolling: null };
  const labels = { subject: '主体', scene: '场景', shot_size: '景别', camera_angle: '角度', composition: '构图', lighting: '光线', color_palette: '色彩', visible_pose: '姿态', visible_text: '画面文字' };
  const summaryLabels = { theme: '主题', visual_style: '视觉风格', content_structure: '内容结构' };
  const intervalLabels = { visual_change: '画面变化', subject_motion: '主体运动', camera_motion: '运镜', transition: '转场' };
  const scoreLabels = { static_description: '静态描述正确性', temporal_reasoning: '时间关系判断', coverage: '关键画面覆盖', summary_evidence: '总结依据充分', prompt_usefulness: '提示词可用性', adaptation: '品牌改编合理性' };
  const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const basisLabel = (basis) => ({ observed: '观察', inferred: '推断', unknown: '未知' }[basis] || '未知');
  const formatTime = (ms) => `${(Number(ms || 0) / 1000).toFixed(1)}s`;
  const claimHTML = (label, claim) => {
    if (!claim) return `<div class="claim-item"><div class="claim-head"><span>${escapeHTML(label)}</span><span class="claim-badge unknown">未知</span></div><div class="claim-value">尚无结果</div></div>`;
    const basis = claim.basis || 'unknown';
    const evidence = (claim.evidence_frame_ids || []).join(', ');
    return `<div class="claim-item"><div class="claim-head"><span>${escapeHTML(label)}</span><span class="claim-badge ${escapeHTML(basis)}">${basisLabel(basis)}</span></div><div class="claim-value">${escapeHTML(claim.value ?? '无法判断')}</div>${claim.note ? `<div class="claim-note">${escapeHTML(claim.note)}${evidence ? ` · ${escapeHTML(evidence)}` : ''}</div>` : ''}</div>`;
  };
  const statusText = (status) => ({ queued: '排队中', extracting: '抽帧中', running: '分析中', completed: '已完成', incomplete: '部分完成', failed: '失败', interrupted: '已中断', pending: '等待', partial: '部分完成', analyzing: '分析中', synthesizing: '汇总中', waiting_transcript: '等待转写', waiting_provider: '等待转写服务', no_audio: '无音轨', no_speech: '无口播', skipped: '按范围跳过', unavailable: '不可用', visual: '画面分析', copy_tags: '文案与标签', hook: '前三秒' }[status] || status || '等待');

  async function api(url, options = {}) {
    const response = await fetch(appPath(url), options);
    const contentType = response.headers.get('content-type') || '';
    const data = contentType.includes('application/json') ? await response.json() : await response.text();
    if (!response.ok) throw new Error(data?.error?.message || data?.detail || `请求失败：HTTP ${response.status}`);
    return data;
  }

  async function loadHealth() {
    try {
      const health = await api('/api/health');
      if (APP_READ_ONLY) {
        $('#deployment-label').textContent = '只读演示';
        $('#model-badge').textContent = '不触发模型调用';
        ['#upload-section-head','#dropzone','.brief-title','.brief-grid','.submit-row'].forEach((selector) => $(selector)?.classList.add('hidden'));
        $('#read-only-note').classList.remove('hidden');
        $('#content-read-only-note').classList.remove('hidden');
        ['#content-video-file','#content-transcribe','#transcript-format','#transcript-basis','#transcript-reviewed','#transcript-content','#transcript-import','#content-scope','#content-origin','#content-analyze','#content-review-verdict','#content-reviewer','#review-transcript','#review-structure','#review-tags','#review-hook','#content-review-notes','#content-review-save'].forEach((selector) => { const node = $(selector); if (node) node.disabled = true; });
        $('#content-workflow').classList.add('readonly-content');
      } else {
        $('#model-badge').textContent = `${health.model_id} · ${health.model_configured ? '已配置' : '未配置'}`;
      }
      if (!health.model_configured) $('#model-badge').classList.add('fail-text');
    } catch { $('#model-badge').textContent = '服务连接中'; }
    refreshHistory();
    loadContentCapabilities();
    refreshContentHistory();
  }

  async function refreshHistory() {
    try {
      const response = await api('/api/comparisons');
      const items = response.items || [];
      if (!items.length) { $('#history-bar').classList.add('hidden'); return; }
      $('#history-list').innerHTML = items.map((item) => {
        const time = item.created_at ? new Date(item.created_at * 1000).toLocaleString('zh-CN', { month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' }) : '';
        return `<button class="history-item ${item.comparison_id === state.comparisonId ? 'active' : ''}" data-load="${escapeHTML(item.comparison_id)}"><strong>${escapeHTML(item.original_filename || item.comparison_id)}</strong><span>${statusText(item.status)} · ${escapeHTML(time)} · ${escapeHTML(item.model_id || '')}</span></button>`;
      }).join('');
      $('#history-bar').classList.remove('hidden');
      $('#history-list').querySelectorAll('[data-load]').forEach((button) => button.addEventListener('click', () => loadComparison(button.dataset.load)));
    } catch { /* The workbench remains usable if history cannot be read. */ }
  }

  async function loadComparison(comparisonId) {
    try {
      const data = await api(`/api/comparisons/${encodeURIComponent(comparisonId)}`);
      state.comparisonId = comparisonId;
      state.lastComparison = data;
      const metadata = data.video_metadata || {};
      state.video = { video_id: data.video_id, duration_ms: data.manifest?.duration_ms || metadata.duration_ms, width: metadata.width, height: metadata.height, original_filename: metadata.original_filename, sample_interval_ms: data.interval_ms };
      $('#upload-title').textContent = metadata.original_filename || data.video_id;
      $('#upload-copy').textContent = '已载入历史分析';
      $('#upload-state').textContent = '结果已就绪';
      $('#video-meta').innerHTML = `<span class="meta-chip">${formatTime(state.video.duration_ms)} 时长</span><span class="meta-chip">${escapeHTML(state.video.width || '—')} × ${escapeHTML(state.video.height || '—')}</span><span class="meta-chip">${data.manifest?.frames?.length || 0} 张抽帧</span>`;
      $('#sampling-description').textContent = `本次每 ${(data.interval_ms / 1000).toFixed(2)} 秒 1 帧 · ${data.manifest?.frames?.length || '—'} 帧`;
      $('#video-meta').classList.remove('hidden');
      $('#video-preview').src = appPath(`/api/videos/${encodeURIComponent(data.video_id)}/content`);
      $('#video-preview-wrap').classList.remove('hidden');
      $('#results').classList.remove('hidden');
      $('#run-button').disabled = !['completed','incomplete','failed','interrupted'].includes(data.status);
      if (state.polling) { clearInterval(state.polling); state.polling = null; }
      renderComparison(data);
      loadRelatedContentAnalysis(data.video_id, comparisonId);
      if (!['completed','incomplete','failed','interrupted'].includes(data.status)) state.polling = setInterval(pollComparison, 1200);
      refreshHistory();
      $('#results').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (error) { showNotice(error.message); }
  }

  async function loadRelatedContentAnalysis(videoId, comparisonId = null) {
    if (!videoId) return;
    try {
      const query = new URLSearchParams({ video_id: videoId, limit: '1' });
      if (comparisonId) query.set('comparison_id', comparisonId);
      const data = await api(`/api/content-analyses?${query.toString()}`);
      const item = data.items?.[0];
      state.relatedContentId = item?.analysis_id || null;
      const button = $('#run-content-from-comparison');
      if (item?.analysis_id) {
        button.textContent = `查看文案分析 · ${statusText(item.status)}`;
        button.classList.remove('hidden');
        button.dataset.contentLoad = item.analysis_id;
      } else {
        button.textContent = '基于此结果分析文案';
        button.dataset.contentLoad = '';
      }
    } catch { /* visual results remain usable if the optional workflow is unavailable */ }
  }

  async function runContentFromComparison() {
    if (!state.comparisonId) return;
    if (state.relatedContentId) {
      document.querySelector('[data-workflow="content"]').click();
      await loadContentAnalysis(state.relatedContentId);
      return;
    }
    if (APP_READ_ONLY) return;
    const button = $('#run-content-from-comparison');
    button.disabled = true;
    button.textContent = '正在创建文案分析…';
    try {
      const created = await api(`/api/comparisons/${encodeURIComponent(state.comparisonId)}/content-analysis`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope_hint: 'auto', origin_hint: 'unknown' }),
      });
      state.relatedContentId = created.analysis_id;
      state.analysisId = created.analysis_id;
      document.querySelector('[data-workflow="content"]').click();
      await loadContentAnalysis(created.analysis_id);
      if (['queued','analyzing','visual','copy_tags','hook'].includes((await api(`/api/content-analyses/${encodeURIComponent(created.analysis_id)}`)).status)) {
        state.contentPolling = setInterval(pollContentAnalysis, 1500);
      }
      refreshContentHistory();
    } catch (error) {
      showNotice(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = state.relatedContentId ? `查看文案分析 · ${statusText('completed')}` : '基于此结果分析文案';
    }
  }

  function showNotice(message, error = true) {
    const node = $('#notice');
    node.textContent = message;
    node.classList.toggle('error-notice', error);
    node.classList.remove('hidden');
  }

  async function handleFile(file) {
    if (!file) return;
    $('#notice').classList.add('hidden');
    $('#upload-title').textContent = file.name;
    $('#upload-copy').textContent = `${(file.size / 1024 / 1024).toFixed(1)} MiB · 正在读取视频信息`;
    $('#upload-state').textContent = '上传中…';
    $('#run-button').disabled = true;
    state.video = null;
    try {
      const form = new FormData(); form.append('file', file);
      const video = await api('/api/videos', { method: 'POST', body: form });
      state.video = { ...video, sample_interval_ms: video.recommended_sample_interval_ms };
      $('#upload-copy').textContent = `${video.original_filename} · ${(video.size_bytes / 1024 / 1024).toFixed(1)} MiB`;
      $('#upload-state').textContent = '视频已就绪';
      $('#upload-state').style.color = '#398366';
      $('#video-meta').innerHTML = `<span class="meta-chip">${formatTime(video.duration_ms)} 时长</span><span class="meta-chip">${escapeHTML(video.width)} × ${escapeHTML(video.height)}</span><span class="meta-chip">${video.recommended_frame_count} 张抽帧 · 间隔 ${(video.recommended_sample_interval_ms / 1000).toFixed(2)} 秒</span>`;
      $('#sampling-description').textContent = `每 ${(video.recommended_sample_interval_ms / 1000).toFixed(2)} 秒 1 帧 · ${video.recommended_frame_count} 帧`;
      $('#video-meta').classList.remove('hidden');
      $('#video-preview').src = appPath(`/api/videos/${encodeURIComponent(video.video_id)}/content`);
      $('#video-preview-wrap').classList.remove('hidden');
      $('#run-button').disabled = false;
      $('#results').classList.add('hidden');
    } catch (error) {
      $('#upload-state').textContent = '上传失败';
      showNotice(error.message);
      $('#video-meta').classList.add('hidden');
      $('#video-preview-wrap').classList.add('hidden');
    }
  }

  function getTargetBrief() {
    const selling = $('#selling-points').value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
    const brief = {
      brand_name: $('#brand-name').value.trim(), product_name: $('#product-name').value.trim(),
      theme: $('#target-theme').value.trim(), verified_selling_points: selling,
      replace_instructions: $('#replace-instructions').value.trim(),
    };
    return Object.values(brief).some((value) => Array.isArray(value) ? value.length : value) ? brief : null;
  }

  async function runComparison() {
    if (!state.video) return;
    $('#run-button').disabled = true;
    $('#notice').classList.add('hidden');
    $('#results').classList.remove('hidden');
    ['#method-switch','#evidence-details','#frames-panel','#intervals-panel','#reports-panel','#review-panel','#export-actions','#result-meta'].forEach((selector) => $(selector).classList.add('hidden'));
    $('#progress-panel').classList.remove('hidden');
    $('#comparison-short').textContent = '创建中';
    $('#progress-label').textContent = '正在创建对照任务';
    $('#progress-detail').textContent = '抽帧与模型分析即将开始';
    setProgress(3);
    try {
      const created = await api('/api/comparisons', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ video_id: state.video.video_id, sample_interval_ms: state.video.sample_interval_ms || 1000, target_brief: getTargetBrief() }),
      });
      state.comparisonId = created.comparison_id;
      refreshHistory();
      $('#comparison-short').textContent = created.comparison_id.slice(-10);
      if (state.polling) clearInterval(state.polling);
      await pollComparison();
      state.polling = setInterval(pollComparison, 1200);
    } catch (error) {
      $('#progress-label').textContent = '任务创建失败';
      $('#progress-detail').textContent = error.message;
      $('#spinner').classList.add('stopped');
      $('#run-button').disabled = false;
    }
  }

  function setProgress(percent) { $('#progress-bar').style.width = `${Math.max(0, Math.min(100, percent))}%`; }

  async function pollComparison() {
    if (!state.comparisonId) return;
    try {
      const data = await api(`/api/comparisons/${encodeURIComponent(state.comparisonId)}`);
      state.lastComparison = data;
      renderComparison(data);
      if (['completed','incomplete','failed','interrupted'].includes(data.status)) {
        clearInterval(state.polling); state.polling = null;
        $('#spinner').classList.add('stopped');
        $('#run-button').disabled = false;
        refreshHistory();
      }
    } catch (error) {
      $('#progress-detail').textContent = error.message;
    }
  }

  function renderComparison(data) {
    const done = ['completed','incomplete','failed','interrupted'].includes(data.status);
    const status = $('#overall-status');
    status.textContent = statusText(data.status);
    status.className = `live-status ${done && data.status === 'completed' ? 'done' : done ? 'fail' : 'running'}`;
    const manifest = data.manifest;
    const a = data.branches?.A || {}; const b = data.branches?.B || {};
    const aTotal = a.progress?.total || manifest?.frames?.length || 0;
    const aDone = a.progress?.completed || 0;
    let progress = data.status === 'queued' ? 4 : data.status === 'extracting' ? 15 : 22 + (aTotal ? 40 * (aDone / aTotal) : 0);
    if (a.status === 'completed' || a.status === 'partial' || a.status === 'failed') progress = 65;
    if (b.stage === 'synthesis') progress = 87;
    if (done) progress = 100;
    setProgress(progress);
    $('#progress-label').textContent = data.status === 'extracting' ? '正在抽取共享画面' : data.stage === 'A' ? '方案 A：逐帧独立分析' : data.stage === 'B' ? '方案 B：有序多帧联合分析' : done ? `任务${statusText(data.status)}` : '等待分析';
    $('#progress-detail').textContent = manifest ? `${manifest.frames.length} 帧 · ${aDone}/${aTotal} 张已完成 A 分析` : '正在检查视频并生成帧清单';
    $('#branch-a-status').textContent = `${statusText(a.stage || a.status)}${aTotal ? ` · ${aDone}/${aTotal}` : ''}`;
    $('#branch-b-status').textContent = statusText(b.stage || b.status);
    $('#tab-a-status').textContent = `${statusText(a.stage || a.status)}${aTotal ? ` · ${aDone}/${aTotal}` : ''}`;
    $('#tab-b-status').textContent = statusText(b.stage || b.status);
    $('#export-actions').classList.toggle('hidden', !done);
    $('#run-content-from-comparison').classList.toggle('hidden', APP_READ_ONLY || !done || !data.comparison_id);
    if (manifest) {
      $('#method-switch').classList.remove('hidden');
      $('#evidence-details').classList.remove('hidden');
      $('#evidence-summary').textContent = `${manifest.frames?.length || 0} 帧 · 逐帧和相邻节点按需查看`;
      renderMetadata(data);
      renderFrames(data);
      renderIntervals(data);
      renderReports(data);
      renderReview(data.review);
    } else {
      ['#method-switch','#evidence-details','#frames-panel','#intervals-panel','#reports-panel','#review-panel','#result-meta'].forEach((selector) => $(selector).classList.add('hidden'));
    }
    if (done) {
      $('#progress-label').textContent = `任务${statusText(data.status)}`;
      $('#progress-detail').textContent = data.error?.message || (data.usage ? `累计 ${data.usage.total_tokens || 0} tokens · 方案 A ${a.wall_ms ?? '—'} ms · 方案 B ${b.wall_ms ?? '—'} ms` : '可以检查和导出本次结果');
    }
  }

  function renderMetadata(data) {
    const manifest = data.manifest || {};
    const calls = (data.branches?.A?.calls?.length || 0) + (data.branches?.B?.calls?.length || 0);
    const normalizations = [...(data.branches?.A?.calls || []), ...(data.branches?.B?.calls || [])].reduce((sum, call) => sum + (call.application_normalizations?.length || 0), 0);
    const chips = [`${manifest.frames?.length || 0} 帧`, `${formatTime(manifest.duration_ms)} 视频`, `${manifest.sample_interval_ms || data.interval_ms} ms 间隔`, `${calls} 次调用`, `${data.usage?.total_tokens || 0} tokens`, `Prompt ${data.prompt_version}`, `Schema ${data.schema_version}`];
    if (manifest.tail_unobserved_ms) chips.push(`尾部约 ${manifest.tail_unobserved_ms} ms 未采样`);
    if (normalizations) chips.push(`${normalizations} 处重复证据 ID 已去重并留痕`);
    $('#result-meta').innerHTML = chips.map((chip) => `<span class="meta-chip">${escapeHTML(chip)}</span>`).join('');
    $('#result-meta').classList.remove('hidden');
  }

  function renderFrames(data) {
    const manifest = data.manifest; if (!manifest?.frames) return;
    const method = state.method;
    const branch = data.branches[method] || {};
    const frameMap = new Map((branch.frames || []).map((frame) => [frame.frame_id, frame]));
    if (method === 'A') {
      $('#frame-title').textContent = '逐帧独立分析';
      $('#frame-method').textContent = '方案 A · 每张图片单独识别；点击帧时间定位原视频';
      $('#frame-results').innerHTML = manifest.frames.map((frame) => frameRowHTML(frame, manifest, frameMap.get(frame.frame_id), branch)).join('');
    } else if (branch.temporal_segments?.length) {
      $('#frame-title').textContent = '有序片段与时间推进';
      $('#frame-method').textContent = `方案 B · 全序列归纳为 ${branch.temporal_segments.length} 个连续片段；边界按采样帧估计，不是精确剪辑点`;
      $('#frame-results').innerHTML = branch.temporal_segments.map((segment, index, segments) => segmentHTML(segment, index, segments[index + 1]?.start_ms, manifest, frameMap)).join('');
    } else if (['pending','running','analyzing','synthesizing'].includes(branch.status)) {
      $('#frame-title').textContent = '正在联合分析时间片段';
      $('#frame-method').textContent = '方案 B · 模型正在结合整组有序抽帧归纳片段';
      $('#frame-results').innerHTML = '<div class="empty-state">片段范围与时间推进分析完成后会显示在这里。</div>';
    } else if (data.schema_version !== '1.1') {
      $('#frame-title').textContent = '旧版有序多帧结果';
      $('#frame-method').textContent = '该历史任务未生成片段字段；下方保留原逐帧结果';
      $('#frame-results').innerHTML = `<div class="legacy-segment-note"><b>此历史任务使用旧版输出结构</b><span>旧结果只有逐帧记录和相邻帧变化，无法从现有文字可靠还原模型的片段判断。新运行会生成片段范围、片段内容和时序判断。</span></div><details class="legacy-frame-details"><summary>查看旧版逐帧结果 · ${manifest.frames.length} 帧</summary><div class="legacy-frame-list">${manifest.frames.map((frame) => frameRowHTML(frame, manifest, frameMap.get(frame.frame_id), branch)).join('')}</div></details>`;
    } else {
      $('#frame-title').textContent = '片段分析未返回';
      $('#frame-method').textContent = '新版契约要求模型输出完整片段；此任务记录了错误';
      $('#frame-results').innerHTML = `<div class="failure-box">${escapeHTML(branch.error?.message || '没有有效的片段结果。')}</div>`;
    }
    updateMethodTabs();
    $('#frames-panel').classList.remove('hidden');
    $('#frame-results').querySelectorAll('[data-seek]').forEach((button) => button.addEventListener('click', () => {
      const video = $('#video-preview'); video.currentTime = Number(button.dataset.seek) / 1000; video.play().catch(() => {});
      video.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }));
  }

  function frameRowHTML(frame, manifest, observation, branch) {
    const imageURL = appPath(`/api/frame-sets/${encodeURIComponent(manifest.frame_set_id)}/frames/${encodeURIComponent(frame.frame_id)}`);
    const claims = observation
      ? `<div class="claim-grid">${Object.keys(labels).map((key) => claimHTML(labels[key], observation[key])).join('')}</div>`
      : frameResultPlaceholder(branch, frame.frame_id);
    return `<details class="frame-row"><summary class="frame-top"><img class="frame-thumb" src="${imageURL}" alt="${escapeHTML(frame.frame_id)} 抽帧" loading="lazy"><span class="frame-seek" data-seek="${frame.timestamp_ms}">${escapeHTML(frame.frame_id)} · ${formatTime(frame.timestamp_ms)}</span><span>${escapeHTML(frame.width)} × ${escapeHTML(frame.height)}</span><a class="frame-open" href="${imageURL}" target="_blank" rel="noopener">查看原图 ↗</a><span class="frame-toggle" aria-hidden="true">展开</span></summary><div class="frame-content">${claims}</div></details>`;
  }

  function segmentHTML(segment, index, nextSegmentStartMs, manifest, frameMap) {
    const positions = new Map(manifest.frames.map((frame, frameIndex) => [frame.frame_id, frameIndex]));
    const startIndex = positions.get(segment.start_frame_id);
    const endIndex = positions.get(segment.end_frame_id);
    if (startIndex == null || endIndex == null || startIndex > endIndex) return '<div class="failure-box">片段范围与抽帧清单不匹配。</div>';
    const frames = manifest.frames.slice(startIndex, endIndex + 1);
    const strip = frames.map((frame) => {
      const imageURL = appPath(`/api/frame-sets/${encodeURIComponent(manifest.frame_set_id)}/frames/${encodeURIComponent(frame.frame_id)}`);
      return `<button class="segment-frame-chip" type="button" data-seek="${frame.timestamp_ms}" title="定位到 ${formatTime(frame.timestamp_ms)}"><img src="${imageURL}" alt="${escapeHTML(frame.frame_id)}"><span>${formatTime(frame.timestamp_ms)}</span></button>`;
    }).join('');
    const details = frames.map((frame) => {
      const imageURL = appPath(`/api/frame-sets/${encodeURIComponent(manifest.frame_set_id)}/frames/${encodeURIComponent(frame.frame_id)}`);
      const observation = frameMap.get(frame.frame_id);
      const claims = observation
        ? `<div class="claim-grid">${Object.keys(labels).map((key) => claimHTML(labels[key], observation[key])).join('')}</div>`
        : frameResultPlaceholder({ status: 'completed' }, frame.frame_id);
      return `<article class="segment-detail-frame"><div class="segment-detail-head"><img src="${imageURL}" alt="${escapeHTML(frame.frame_id)}" loading="lazy"><button type="button" data-seek="${frame.timestamp_ms}">${escapeHTML(frame.frame_id)} · ${formatTime(frame.timestamp_ms)}</button></div>${claims}</article>`;
    }).join('');
    const displayEndMs = nextSegmentStartMs ?? segment.end_ms;
    return `<article class="segment-card"><header class="segment-card-head"><div><span class="segment-index">片段 ${String(index + 1).padStart(2, '0')}</span><h3>连续画面与时间变化</h3></div><span class="segment-range">约 ${formatTime(segment.start_ms)}–${formatTime(displayEndMs)} · ${frames.length} 帧</span></header><div class="segment-body"><div class="segment-strip" aria-label="片段内抽帧">${strip}</div><div class="segment-claims">${claimHTML('片段内容',segment.description)}${claimHTML('动作发展',segment.action_progression)}${claimHTML('运镜判断',segment.camera_motion)}${claimHTML('镜头连续性',segment.continuity)}</div><details class="segment-evidence"><summary>展开 ${frames.length} 帧的静态识别证据</summary><div class="segment-detail-list">${details}</div></details></div></article>`;
  }

  function frameResultPlaceholder(branch, frameId) {
    const error = (branch.errors || []).find((item) => item.frame_id === frameId)?.message || branch.error?.message;
    if (error) return `<div class="failure-box">${escapeHTML(error)}</div>`;
    if (['pending','running','analyzing','synthesizing'].includes(branch.status) || !branch.status) return `<div class="empty-state">该帧结果尚在处理中</div>`;
    return '<div class="failure-box">该方案没有返回此帧结果。</div>';
  }

  function renderIntervals(data) {
    const method = state.method;
    const branch = data.branches[method] || {};
    const report = branch.report;
    $('#interval-method').textContent = method === 'A' ? '方案 A · 根据独立帧描述归纳' : branch.temporal_segments?.length ? '方案 B · 每对抽帧细节已收起，片段归纳见上方' : '方案 B · 模型联合观察相邻画面';
    const frameCount = data.manifest.frames.length;
    if (frameCount < 2) { $('#interval-results').innerHTML = '<div class="empty-state">只有一张抽帧，无法构成相邻区间。</div>'; $('#intervals-panel').classList.remove('hidden'); return; }
    const intervals = new Map((report?.intervals || []).map((item) => [`${item.from_frame_id}/${item.to_frame_id}`, item]));
    const sequence = data.manifest.frames.slice(0,-1).map((frame,index) => ({from:frame,to:data.manifest.frames[index+1]}));
    const rows = sequence.map(({from,to})=>{
      const key=`${from.frame_id}/${to.frame_id}`; const interval=intervals.get(key);
      return `<div class="interval-row"><div class="interval-time">${formatTime(from.timestamp_ms)} → ${formatTime(to.timestamp_ms)}</div><div class="interval-col">${intervalHTML(interval)}</div></div>`;
    }).join('');
    $('#interval-results').innerHTML = method === 'B' && branch.temporal_segments?.length
      ? `<details class="pairwise-details"><summary>展开 ${sequence.length} 对相邻抽帧的变化、动作与运镜判断</summary><div class="pairwise-list">${rows}</div></details>`
      : rows;
    $('#intervals-panel').classList.remove('hidden');
  }
  function intervalHTML(interval) {
    if (!interval) return '<span class="empty-state">等待区间分析</span>';
    return Object.keys(intervalLabels).map((key)=>{const claim=interval[key]; return `<div class="interval-claim"><b>${escapeHTML(intervalLabels[key])} · ${basisLabel(claim?.basis)}</b>${escapeHTML(claim?.value ?? '无法判断')}${claim?.note ? `<span class="claim-note"> · ${escapeHTML(claim.note)}</span>` : ''}</div>`;}).join('');
  }

  function renderReports(data) {
    const method = state.method;
    const branch = data.branches[method] || {};
    const label = method === 'A' ? '单帧独立分析' : '有序多帧分析';
    $('#report-method').textContent = `方案 ${method} · ${label}`;
    $('#report-results').innerHTML = `<article class="report-card">${reportCardHTML(method,label,branch)}</article>`;
    $('#reports-panel').classList.remove('hidden');
    $('#report-results').querySelectorAll('[data-copy]').forEach((button)=>button.addEventListener('click', async()=>{
      try { await navigator.clipboard.writeText(button.dataset.copy); button.textContent='已复制'; setTimeout(()=>button.textContent='复制',1000); }
      catch { button.textContent='复制失败'; }
    }));
  }

  function updateMethodTabs() {
    document.querySelectorAll('[data-method]').forEach((button) => {
      const selected = button.dataset.method === state.method;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
  }

  function selectMethod(method) {
    if (!['A','B'].includes(method) || state.method === method) return;
    state.method = method;
    history.replaceState(null, '', `${location.pathname}${location.search}#method=${method}`);
    updateMethodTabs();
    if (state.lastComparison?.manifest) {
      renderFrames(state.lastComparison);
      renderIntervals(state.lastComparison);
      renderReports(state.lastComparison);
    }
  }
  function reportCardHTML(letter,label,branch) {
    const report=branch.report;
    if (!report) return `<div class="report-card-head"><div><b>${letter}</b><strong>${label}</strong></div><span>${statusText(branch.status)}</span></div>${branch.error ? `<div class="report-body"><div class="failure-box">${escapeHTML(branch.error.message)}</div></div>` : '<div class="report-body"><div class="empty-state">全片报告生成后显示</div></div>'}`;
    const summary=report.summary||{};
    const claims=Object.keys(summaryLabels).map((key)=>`<div class="report-claim">${claimHTML(summaryLabels[key],summary[key])}</div>`).join('');
    const features=(summary.reusable_features||[]).map((feature)=>`<span class="feature-chip"><span class="claim-badge ${escapeHTML(feature.basis)}">${basisLabel(feature.basis)}</span> ${escapeHTML(feature.value??'无法判断')}</span>`).join('');
    const source=report.source_prompt||''; const adapted=report.adapted_prompt||'';
    return `<div class="report-card-head"><div><b>${letter}</b><strong>${label}</strong></div><span>${branch.wall_ms??'—'} ms · ${(branch.calls||[]).length} calls</span></div><div class="report-body"><div class="report-subtitle">全片特征</div>${claims}<div class="report-subtitle">可复用特征</div><div class="features-list">${features||'<span class="empty-state">暂无可复用特征</span>'}</div><div class="report-subtitle">参考视频提示词 <button class="copy-button" data-copy="${escapeHTML(source)}">复制</button></div><div class="prompt-box">${escapeHTML(source)}</div><div class="report-subtitle">品牌改编提示词 <button class="copy-button" data-copy="${escapeHTML(adapted)}" ${adapted?'':'disabled'}>复制</button></div><div class="prompt-box">${escapeHTML(adapted||'未填写目标资料，因此未生成改编提示词。')}</div>${report.adaptation_notes?.length?`<div class="report-subtitle">改编说明</div><ul class="report-limits">${report.adaptation_notes.map((note)=>`<li>${escapeHTML(note)}</li>`).join('')}</ul>`:''}<div class="report-subtitle">限制说明</div><ul class="report-limits">${(report.limitations||[]).map((item)=>`<li>${escapeHTML(item)}</li>`).join('')}</ul></div>`;
  }

  function renderReview(review) {
    if (APP_READ_ONLY) return;
    if (!$('#review-scores').childElementCount) {
      $('#review-scores').innerHTML = Object.entries(scoreLabels).map(([key,label])=>`<div class="score-item"><label for="score-${key}">${label}</label><select id="score-${key}" data-score="${key}"><option value="">N/A · 无法判断</option>${[1,2,3,4,5].map((n)=>`<option value="${n}">${n}</option>`).join('')}</select></div>`).join('');
    }
    if (review) {
      Object.entries(review.scores||{}).forEach(([key,value])=>{const select=$(`[data-score="${key}"]`);if(select)select.value=value??'';});
      $('#review-preference').value=review.preference||'';$('#evidence-note').value=review.evidence_note||'';$('#reviewer').value=review.reviewer||'';
    }
    $('#review-panel').classList.remove('hidden');
  }

  async function saveReview() {
    if (!state.comparisonId) return;
    const scores={};document.querySelectorAll('[data-score]').forEach((select)=>{scores[select.dataset.score]=select.value?Number(select.value):null;});
    $('#save-review').disabled=true;$('#save-status').textContent='保存中…';
    try {
      await api(`/api/comparisons/${encodeURIComponent(state.comparisonId)}/review`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({scores,preference:$('#review-preference').value,evidence_note:$('#evidence-note').value,reviewer:$('#reviewer').value})});
      $('#save-status').textContent='已保存';
    } catch(error) { $('#save-status').textContent=error.message; }
    finally { $('#save-review').disabled=false; }
  }

  function download(format) { if (state.comparisonId) window.location.href=appPath(`/api/comparisons/${encodeURIComponent(state.comparisonId)}/export?format=${format}`); }

  function setContentNotice(message, error = false) {
    const node = $('#content-notice');
    node.textContent = message || '';
    node.classList.toggle('error-notice', error);
  }

  async function loadContentCapabilities() {
    try {
      const data = await api('/api/capabilities');
      const asr = data.asr || {};
      const stateLabel = asr.status === 'development_only' ? '开发模拟' : asr.configured ? `${asr.provider} · 已连接` : asr.status === 'adapter_not_implemented' ? `${asr.provider} · 适配器待实现` : 'ASR 未配置';
      $('#content-capability').textContent = `${stateLabel} · 文案模型 ${data.analysis_model?.configured ? '已配置' : '未配置'}`;
      $('#content-capability-detail').innerHTML = `<div><b>语音转写</b><span>${escapeHTML(stateLabel)}</span></div><div><b>时间精度</b><span>${escapeHTML(asr.timestamp_granularity || 'unknown')}</span></div><div><b>文案分析模型</b><span>${escapeHTML(data.analysis_model?.model || '—')} · ${data.analysis_model?.configured ? '已配置' : '未配置'}</span></div><div><b>导入格式</b><span>${escapeHTML((data.transcript_import?.formats || []).join(' / ') || '不可用')}</span></div>`;
      $('#content-transcribe').disabled = APP_READ_ONLY || !state.contentVideo || asr.status === 'adapter_not_implemented';
      if (APP_READ_ONLY) $('#content-capability').textContent += ' · 只读';
    } catch (error) {
      $('#content-capability').textContent = '能力状态读取失败';
      $('#content-capability-detail').textContent = error.message;
    }
  }

  async function uploadContentVideo(file) {
    if (!file || APP_READ_ONLY) return;
    state.contentVideo = null; state.transcriptId = null; state.transcriptionJobId = null;
    $('#content-video-status').textContent = '正在上传并读取视频信息…';
    $('#content-video-meta').classList.add('hidden'); $('#content-video-wrap').classList.add('hidden');
    $('#transcript-import').disabled = true; $('#content-analyze').disabled = true;
    setContentNotice('');
    try {
      const form = new FormData(); form.append('file', file);
      const video = await api('/api/videos', { method: 'POST', body: form });
      state.contentVideo = video;
      $('#content-video-status').textContent = `${video.original_filename} · 已就绪`;
      $('#content-video-meta').innerHTML = `<span class="meta-chip">${formatTime(video.duration_ms)} 时长</span><span class="meta-chip">${escapeHTML(video.width)} × ${escapeHTML(video.height)}</span><span class="meta-chip">${(video.size_bytes / 1024 / 1024).toFixed(1)} MiB</span>`;
      $('#content-video-meta').classList.remove('hidden');
      $('#content-video-preview').src = appPath(`/api/videos/${encodeURIComponent(video.video_id)}/content`);
      $('#content-video-wrap').classList.remove('hidden');
      $('#content-transcribe').disabled = false;
      $('#transcript-import').disabled = false;
      updateContentAnalyzeAvailability();
      setContentNotice('视频已就绪。可尝试 ASR；没有接入服务时，也可以导入 SRT / 文本 / JSON。');
    } catch (error) {
      $('#content-video-status').textContent = '上传失败';
      setContentNotice(error.message, true);
    }
  }

  function updateContentAnalyzeAvailability() {
    const scope = $('#content-scope').value;
    $('#content-analyze').disabled = APP_READ_ONLY || !state.contentVideo || (!state.transcriptId && !['pure_display','3d_showcase'].includes(scope));
  }

  async function startTranscription() {
    if (!state.contentVideo || APP_READ_ONLY) return;
    $('#content-transcribe').disabled = true;
    $('#transcript-status').textContent = '正在检查音轨与转写服务…';
    setContentNotice('转写任务已提交。');
    try {
      const created = await api('/api/transcriptions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ video_id: state.contentVideo.video_id }) });
      state.transcriptionJobId = created.transcription_job_id;
      await pollTranscription();
      if (state.transcriptionJobId) state.contentPolling = setInterval(pollTranscription, 1200);
    } catch (error) {
      $('#transcript-status').textContent = error.message;
      setContentNotice(error.message, true);
      $('#content-transcribe').disabled = false;
    }
  }

  async function pollTranscription() {
    if (!state.transcriptionJobId) return;
    try {
      const job = await api(`/api/transcriptions/${encodeURIComponent(state.transcriptionJobId)}`);
      if (['queued','probing','extracting','transcribing'].includes(job.status)) {
        $('#transcript-status').textContent = `转写状态：${statusText(job.status)}`;
        return;
      }
      clearInterval(state.contentPolling); state.contentPolling = null;
      if (job.transcript_id) {
        state.transcriptId = job.transcript_id;
        const transcript = await api(`/api/transcripts/${encodeURIComponent(job.transcript_id)}`);
        $('#transcript-format').value = 'text';
        $('#transcript-basis').value = transcript.content_basis || 'unverified';
        $('#transcript-content').value = (transcript.segments || []).map((segment) => segment.text).join('\n');
        $('#transcript-reviewed').checked = Boolean(transcript.reviewed_by_human);
        $('#transcript-status').textContent = job.status === 'no_audio' ? '已确认无音轨；可直接跳过或导入文本。' : `转写可用：${(transcript.segments || []).length} 段`;
      } else {
        $('#transcript-status').textContent = job.error?.message || `转写状态：${statusText(job.status)}`;
        setContentNotice(job.error?.message || '转写没有产出文本；可导入人工校对稿继续。', job.status === 'failed');
      }
      updateContentAnalyzeAvailability();
      refreshContentHistory();
    } catch (error) {
      clearInterval(state.contentPolling); state.contentPolling = null;
      $('#transcript-status').textContent = error.message;
      setContentNotice(error.message, true);
    } finally {
      $('#content-transcribe').disabled = APP_READ_ONLY || !state.contentVideo;
    }
  }

  async function importTranscript() {
    if (!state.contentVideo || APP_READ_ONLY) return;
    const raw = $('#transcript-content').value.trim();
    if (!raw) { setContentNotice('请先粘贴待导入的文本。', true); return; }
    let content = raw;
    if ($('#transcript-format').value === 'json') {
      try { content = JSON.parse(raw); } catch { setContentNotice('JSON 格式解析失败，请检查内容。', true); return; }
    }
    $('#transcript-import').disabled = true;
    $('#transcript-status').textContent = '正在校验并保存文本…';
    try {
      const data = await api('/api/transcripts/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ video_id: state.contentVideo.video_id, format: $('#transcript-format').value, content, content_basis: $('#transcript-basis').value, reviewed_by_human: $('#transcript-reviewed').checked }) });
      state.transcriptId = data.transcript_id;
      const transcript = data.transcript;
      $('#transcript-status').textContent = `已保存 ${transcript.segments.length} 段 · ${transcript.timing === 'none' ? '无时间戳' : transcript.timing === 'word' ? '词级时间' : '段落时间'}`;
      setContentNotice('文本已保存，现可创建分析任务。');
      updateContentAnalyzeAvailability();
      refreshContentHistory();
    } catch (error) {
      $('#transcript-status').textContent = error.message;
      setContentNotice(error.message, true);
    } finally { $('#transcript-import').disabled = APP_READ_ONLY || !state.contentVideo; }
  }

  async function createContentAnalysis() {
    if (!state.contentVideo || APP_READ_ONLY) return;
    const scope = $('#content-scope').value;
    $('#content-analyze').disabled = true;
    setContentNotice('正在创建分析任务…');
    $('#content-result').classList.remove('hidden');
    $('#content-report').innerHTML = '<div class="empty-state">任务处理中…</div>';
    try {
      const created = await api('/api/content-analyses', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ video_id: state.contentVideo.video_id, transcript_id: state.transcriptId, scope_hint: scope, origin_hint: $('#content-origin').value }) });
      state.analysisId = created.analysis_id;
      await pollContentAnalysis();
      const current = await api(`/api/content-analyses/${encodeURIComponent(state.analysisId)}`);
      if (['queued','analyzing','visual','copy_tags','hook','waiting_transcript'].includes(current.status)) state.contentPolling = setInterval(pollContentAnalysis, 1500);
      $('#content-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (error) {
      setContentNotice(error.message, true);
      $('#content-report').innerHTML = `<div class="failure-box">${escapeHTML(error.message)}</div>`;
      $('#content-analyze').disabled = false;
    }
  }

  async function pollContentAnalysis() {
    if (!state.analysisId) return;
    try {
      const data = await api(`/api/content-analyses/${encodeURIComponent(state.analysisId)}`);
      renderContentAnalysis(data);
      if (!['queued','analyzing','visual','copy_tags','hook','waiting_transcript'].includes(data.status)) {
        clearInterval(state.contentPolling); state.contentPolling = null;
        $('#content-analyze').disabled = APP_READ_ONLY || !state.contentVideo || (!state.transcriptId && !['pure_display','3d_showcase'].includes($('#content-scope').value));
        refreshContentHistory();
      }
    } catch (error) {
      clearInterval(state.contentPolling); state.contentPolling = null;
      setContentNotice(error.message, true);
    }
  }

  const evidenceText = (claim) => (claim?.evidence || []).map((item) => `${item.type}:${item.id}${item.quote ? ` “${item.quote}”` : ''}`).join(' · ');
  function contentClaimHTML(label, claim) {
    if (!claim) return '';
    const basis = claim.basis || 'unknown';
    return `<article class="content-claim"><header><b>${escapeHTML(label)}</b><span class="claim-badge ${escapeHTML(basis)}">${basisLabel(basis)}</span></header><p>${escapeHTML(claim.value || '无法判断')}</p>${claim.note ? `<small>${escapeHTML(claim.note)}</small>` : ''}${evidenceText(claim) ? `<small class="content-evidence">依据：${escapeHTML(evidenceText(claim))}</small>` : ''}</article>`;
  }

  function renderContentAnalysis(data) {
    const report = data.report;
    const activeLabel = data.stage ? statusText(data.stage) : statusText(data.status);
    $('#content-result-meta').textContent = `${data.analysis_id} · ${activeLabel}${report ? ` · ${report.input_mode} · ${report.provenance?.workflow_version}` : ''}`;
    const stageNodes = { text: $('#stage-text'), structure: $('#stage-structure'), tags: $('#stage-tags'), hook: $('#stage-hook') };
    const stageState = report ? {
      text: report.transcript?.outcome === 'text_available' ? 'done' : 'limited',
      structure: report.copy?.status === 'completed' ? 'done' : 'limited',
      tags: report.tags ? 'done' : 'limited',
      hook: report.hook?.status === 'available' ? 'done' : 'limited',
    } : { text: data.stage === 'waiting_transcript' ? 'limited' : 'running', structure: 'waiting', tags: 'waiting', hook: 'waiting' };
    Object.entries(stageNodes).forEach(([key, node]) => { if (node) node.className = `stage-${stageState[key] || 'waiting'}`; });
    if (!report) {
      $('#content-report').innerHTML = data.error ? `<div class="failure-box">${escapeHTML(data.error.message)}</div>` : `<div class="empty-state">${escapeHTML(activeLabel)} · ${(data.model_calls || []).length} 次模型调用</div>`;
      $('#content-result').classList.remove('hidden');
      return;
    }
    const transcript = report.transcript;
    const visual = report.visual || {};
    const copy = report.copy || {};
    const hook = report.hook || {};
    const tags = report.tags || {};
    const transcriptRows = (transcript?.segments || []).map((seg) => `<li><time>${seg.start_ms == null ? '时间未知' : `${formatTime(seg.start_ms)}–${formatTime(seg.end_ms)}`}</time><span>${escapeHTML(seg.text)}</span></li>`).join('');
    const visualRows = (visual.observations || []).map((item) => `<details class="visual-observation"><summary><img loading="lazy" src="${appPath(`/api/content-analyses/${encodeURIComponent(data.analysis_id)}/frames/${encodeURIComponent(item.frame_id)}`)}" alt="${escapeHTML(item.frame_id)}"><div><b>${escapeHTML(item.frame_id)} · ${formatTime(item.timestamp_ms)}</b><p>${escapeHTML(item.description)}</p></div><span class="frame-toggle">展开</span></summary><div class="visual-observation-detail">${item.onscreen_text?.length ? `<small>画面文字：${escapeHTML(item.onscreen_text.join('；'))}</small>` : '<small>画面文字：未识别</small>'}<small>${(item.limitations || []).map(escapeHTML).join('；')}</small></div></details>`).join('');
    const segments = (copy.segments || []).map((item, index) => `<article class="copy-segment"><header><b>${index + 1}. ${(item.roles || []).map((x) => escapeHTML(x)).join(' / ') || '文案段'}</b><span>${item.start_ms == null ? '时间未知' : `${formatTime(item.start_ms)}–${formatTime(item.end_ms)}`}</span></header><p>${escapeHTML((transcript?.segments || []).filter((seg) => (item.transcript_segment_ids || []).includes(seg.segment_id)).map((seg) => seg.text).join(' '))}</p>${contentClaimHTML('表达目的', item.purpose)}</article>`).join('');
    const formatTags = (tags.format_tags || []).map((item) => `<span class="content-tag">${escapeHTML(item.value)} <small>${basisLabel(item.basis)}</small></span>`).join('');
    const topicTags = (tags.topic_tags || []).map((item) => `<span class="content-tag">${escapeHTML(item.value)}</span>`).join('');
    const techniqueTags = (hook.techniques || []).map((item) => `<span class="content-tag">${escapeHTML(item.type)} · ${escapeHTML(item.evidence?.id || '')}</span>`).join('');
    const warnings = [...(report.limitations || []), ...(transcript?.warnings || []), ...(hook.limitations || [])];
     $('#content-report').innerHTML = `<div class="content-report-grid">
       <section class="content-report-section content-summary-section"><h3>全文总结与文案结构</h3>${contentClaimHTML('内容总结', copy.summary)}${segments || '<div class="empty-state">没有结构化文案段落。</div>'}${copy.structure_pattern ? contentClaimHTML('结构模式', copy.structure_pattern) : ''}</section>
       <section class="content-report-section"><h3>标签分类</h3><div class="tag-row">${formatTags || '<span class="subtle">形式：未知</span>'}</div>${contentClaimHTML('素材来源', tags.origin)}</section>
       <section class="content-report-section"><h3>前三秒分析 <small>${escapeHTML(hook.status || 'limited')} · ${formatTime(hook.window_end_ms)}</small></h3>${contentClaimHTML('口播摘录', hook.spoken_excerpt)}${contentClaimHTML('屏幕文字', hook.onscreen_text)}${contentClaimHTML('开场画面', hook.visual_summary)}<div class="tag-row">${techniqueTags || '<span class="subtle">未识别表达手法</span>'}</div>${contentClaimHTML('吸引点解释', hook.explanation)}</section>
       <section class="content-report-section"><h3>转写 / 字幕原文 <small>${escapeHTML(report.input_mode)} · ${escapeHTML(transcript?.timing || '无时间戳')}</small></h3>${transcriptRows ? `<ol class="transcript-lines">${transcriptRows}</ol>` : '<div class="empty-state">当前报告没有可展示的转写段落。</div>'}</section>
       <details class="content-report-section content-visual-section"><summary><span>抽帧视觉证据</span><small>${visual.available ? `${(visual.observations || []).length} 帧 · 默认收起` : '不可用'}</small></summary>${visualRows || '<div class="empty-state">没有视觉证据。</div>'}</details>
       <section class="content-report-section"><h3>已知局限</h3><ul>${[...new Set(warnings)].map((item) => `<li>${escapeHTML(item)}</li>`).join('') || '<li>无</li>'}</ul><small>模型 ${escapeHTML(report.provenance?.returned_model || '未调用')} · ${escapeHTML(report.provenance?.workflow_version || '')} · fixture ${report.provenance?.is_fixture ? '是' : '否'}</small></section>
      </div>`;
    $('#content-export-json').onclick = () => { window.location.href = appPath(`/api/content-analyses/${encodeURIComponent(data.analysis_id)}/export?format=json`); };
    $('#content-export-md').onclick = () => { window.location.href = appPath(`/api/content-analyses/${encodeURIComponent(data.analysis_id)}/export?format=md`); };
    $('#content-report').querySelectorAll('[data-content-seek]').forEach((button) => button.addEventListener('click', () => { const player = $('#content-video-preview'); player.currentTime = Number(button.dataset.contentSeek) / 1000; player.play().catch(() => {}); player.scrollIntoView({ behavior: 'smooth', block: 'center' }); }));
    $('#content-result').classList.remove('hidden');
  }

  async function refreshContentHistory() {
    try {
      const query = new URLSearchParams();
      if ($('#content-history-format')?.value) query.set('format_tag', $('#content-history-format').value);
      if ($('#content-history-source')?.value) query.set('input_mode', $('#content-history-source').value);
      const data = await api(`/api/content-analyses?${query.toString()}`);
      $('#content-history-list').innerHTML = data.items.length ? data.items.map((item) => `<button type="button" class="content-history-item" data-content-load="${escapeHTML(item.analysis_id)}"><b>${escapeHTML(item.analysis_id)}</b><span>${escapeHTML(item.scope || '—')} · ${escapeHTML(item.input_mode || '—')} · ${escapeHTML(statusText(item.status))}</span><small>${escapeHTML(item.summary || item.video_id || '')}</small></button>`).join('') : '<div class="empty-state">还没有文案分析记录。</div>';
      $('#content-history-list').querySelectorAll('[data-content-load]').forEach((button) => button.addEventListener('click', () => loadContentAnalysis(button.dataset.contentLoad)));
    } catch (error) { $('#content-history-list').innerHTML = `<div class="empty-state">${escapeHTML(error.message)}</div>`; }
  }

  async function loadContentAnalysis(analysisId) {
    try {
      const data = await api(`/api/content-analyses/${encodeURIComponent(analysisId)}`);
      state.analysisId = analysisId;
      renderContentAnalysis(data);
      if (data.video_id) {
        const video = await api(`/api/videos/${encodeURIComponent(data.video_id)}/metadata`);
        state.contentVideo = video;
        $('#content-video-status').textContent = `${video.original_filename} · 历史视频`;
        $('#content-video-meta').innerHTML = `<span class="meta-chip">${formatTime(video.duration_ms)} 时长</span><span class="meta-chip">${escapeHTML(video.width)} × ${escapeHTML(video.height)}</span>`;
        $('#content-video-meta').classList.remove('hidden');
        $('#content-video-preview').src = appPath(`/api/videos/${encodeURIComponent(data.video_id)}/content`);
        $('#content-video-wrap').classList.remove('hidden');
        state.transcriptId = data.transcript_id || data.report?.transcript?.transcript_id || null;
      }
      $('#content-export-json').onclick = () => { window.location.href = appPath(`/api/content-analyses/${encodeURIComponent(analysisId)}/export?format=json`); };
      $('#content-export-md').onclick = () => { window.location.href = appPath(`/api/content-analyses/${encodeURIComponent(analysisId)}/export?format=md`); };
      $('#content-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
      updateContentAnalyzeAvailability();
    } catch (error) { setContentNotice(error.message, true); }
  }

  async function saveContentReview() {
    if (!state.analysisId || APP_READ_ONLY) return;
    const score = (selector) => $(selector).value ? Number($(selector).value) : null;
    $('#content-review-save').disabled = true; $('#content-review-status').textContent = '保存中…';
    try {
      await api(`/api/content-analyses/${encodeURIComponent(state.analysisId)}/review`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ verdict: $('#content-review-verdict').value, reviewer: $('#content-reviewer').value, notes: $('#content-review-notes').value, dimensions: { transcript_fidelity: score('#review-transcript'), structure_usefulness: score('#review-structure'), tags_usefulness: score('#review-tags'), hook_evidence: score('#review-hook') } }) });
      $('#content-review-status').textContent = '已保存';
    } catch (error) { $('#content-review-status').textContent = error.message; }
    finally { $('#content-review-save').disabled = false; }
  }

  document.querySelectorAll('[data-workflow]').forEach((button) => button.addEventListener('click', () => {
    const selected = button.dataset.workflow === 'content' ? 'content' : 'visual';
    document.querySelectorAll('[data-workflow]').forEach((item) => item.classList.toggle('active', item.dataset.workflow === selected));
    $('#visual-workflow').classList.toggle('hidden', selected !== 'visual');
    $('#content-workflow').classList.toggle('hidden', selected !== 'content');
    if (selected === 'content') refreshContentHistory();
  }));
  $('#content-video-file').addEventListener('change',(event)=>uploadContentVideo(event.target.files[0]));
  $('#content-transcribe').addEventListener('click',startTranscription);
  $('#transcript-import').addEventListener('click',importTranscript);
  $('#content-analyze').addEventListener('click',createContentAnalysis);
  $('#content-scope').addEventListener('change',updateContentAnalyzeAvailability);
  $('#content-history-refresh').addEventListener('click',refreshContentHistory);
  $('#content-history-format').addEventListener('change',refreshContentHistory);
  $('#content-history-source').addEventListener('change',refreshContentHistory);
  $('#content-review-save').addEventListener('click',saveContentReview);
  $('#video-file').addEventListener('change',(event)=>handleFile(event.target.files[0]));
  const dropzone=$('#dropzone');
  for(const eventName of ['dragenter','dragover'])dropzone.addEventListener(eventName,(event)=>{event.preventDefault();dropzone.classList.add('dragover');});
  for(const eventName of ['dragleave','drop'])dropzone.addEventListener(eventName,(event)=>{event.preventDefault();dropzone.classList.remove('dragover');});
  dropzone.addEventListener('drop',(event)=>handleFile(event.dataTransfer.files[0]));
  $('#run-button').addEventListener('click',runComparison);
  $('#run-content-from-comparison').addEventListener('click',runContentFromComparison);
  $('#save-review').addEventListener('click',saveReview);
  $('#export-md').addEventListener('click',()=>download('md'));
  $('#export-json').addEventListener('click',()=>download('json'));
  $('#refresh-history').addEventListener('click',refreshHistory);
  document.querySelectorAll('[data-method]').forEach((button) => button.addEventListener('click', () => selectMethod(button.dataset.method)));
  loadHealth();
  refreshContentHistory();
})();
