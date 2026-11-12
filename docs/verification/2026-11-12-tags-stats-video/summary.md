---
title: 标签系统 / 统计室 / X 视频帖验收
description: 2026-11-12 三个迭代（schema v10/v11）的隔离实例浏览器验收证据与覆盖项。
date: 2026-11-12
---

# 验收：标签系统 · 统计室 · X 视频帖（2026-11-12）

## 环境

- 隔离实例：`PICWALL_DATA_DIR=/tmp/pw-accept` + `app.serve.LocalOwner` 包装，127.0.0.1:18081，v0.5.7 / schema v11。
- 数据：`demo.seed()` 演示库 + 经真实契约链路（`xapi._tweet_to_item → x.normalize_tweet → crawler._store_post`，patch 下载层）入库的 1 帖 X 视频帖（mp4 变体 + 海报）。
- 测试基线：307 后端 + 56 前端 + ruff 全绿（`make picwall-test` 同款三连）。

## 覆盖项

| 项 | 预期 | 实测 | 结果 |
|---|---|---|---|
| 视频墙卡 | 海报静帧 + 「▶ 0:47」时长徽标 + X 来源标 | wall-video-card.png / 首页快照均见 | ✅ |
| 视频详情 | `<video controls poster>` 接管舞台，poster=海报缩略图、src=mp4 | evaluate 取值一致（detail-video-tags.png） | ✅ |
| 详情打标 | 「＋ 标签」→ 行内输入回车 → chip「验收 ✕」落库（库内 `tags=['验收']`、decision_version 不动） | 实测落库 + 渲染 | ✅ |
| 标签筛选深链 | `?tags=验收` → 9 帖滤成 1 帖、漏斗徽标 1、URL 同步 | evaluate 断言 | ✅ |
| 筛选面板标签行 | 「标签」行 chips：全部★ / 验收 1★（带帖数与选中态） | filter-panel-tags.png | ✅ |
| 视频大图 | 空格开大图：#lb-video 显示（controls）、#lb-img 隐藏、切走停播 | evaluate 断言（lightbox-video.png） | ✅ |
| 统计页 | 总账 8 块 / 近 30 天 30 列双柱 + 图例 / Top 博主 8 名 / TOP 9 空态 / 回看累计 | stats.png，`/api/stats` 口径与库数一致（含标签数 1） | ✅ |
| 回归 | 全量三连 307+56+ruff 全绿；test_docs 环境变量契约（新增 PICWALL_MAX_VIDEO_BYTES）通过 | CI 同款命令本地跑 | ✅ |

## 边界与留白

- 微博视频仍跳过（本轮边界，README 已知边界已更新）。
- 放映中视频以海报静帧出场（v1 设计，未做内嵌播放器）。
- 验收数据里保留/收藏计数为 0（demo 帖未分拣），TOP 9 呈空态文案——空态本身即验证项。
- 真实 X Cookie 的线上视频抓取未在本轮验收（依赖外部凭据），解析/入库/渲染链路均以契约级 fixture 覆盖。

## 测试

- 新增：`tests/test_tags.py`（11）、`tests/test_stats.py`（4）、`tests/test_video.py`（13）；frontend.test.mjs +1（matchesView 标签）+1 处断言更新。
- 运行：`python -m unittest discover -s tests -t .`（307 全绿）、`node --test tests/frontend.test.mjs`（56 全绿）、`ruff check app tests tools scripts`（0 告警）。
