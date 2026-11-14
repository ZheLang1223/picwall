# Picwall — 微博/X 分组图片墙（仓库自包含 Makefile）
# 常用入口：make picwall（重建+拉起） · make picwall-test（单测+lint） · make picwall-logs
# 约定：宿主侧工具走 .venv（不存在时回落 python3）；维护类命令经 tools/runtime.py
# 自动委派进运行中的容器，与线上服务共享同一份数据与文件锁。

COMPOSE := docker compose
PY := $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
# 宿主侧工具必须显式指定数据目录：默认会落 ~/Library/Application Support/picwall（config.py
# _default_data_dir），打出一份与真实相册无关的空快照；同时 runtime.delegate 靠它与容器挂载
# 对齐来判定「可委派进容器」。容器内不需要（compose 已挂 ./data:/app/data）。
export PICWALL_DATA_DIR := $(CURDIR)/data

.PHONY: picwall picwall-stop picwall-logs picwall-test picwall-preview picwall-benchmark \
        picwall-backup picwall-restore-test picwall-health picwall-repair picwall-cloud \
        picwall-pair picwall-collector-token picwall-safari-cookie picwall-audit \
        extension status atlas-verify backup

## 重建 + 拉起（改 .env 后也跑这条；重打包扩展 zip，注入局域网 IP，等健康检查通过）
picwall: extension
	@LAN_IP=$$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true); \
	PICWALL_LAN_IP="$$LAN_IP" $(COMPOSE) up -d --build
	@echo "等待服务健康…"; \
	i=1; while [ $$i -le 30 ]; do \
	  curl -sf http://127.0.0.1:8081/api/health >/dev/null && { echo "PWALL 已就绪：http://localhost:8081"; exit 0; }; \
	  sleep 2; i=$$((i+1)); \
	done; echo "健康检查超时，看 make picwall-logs"; exit 1
	@# 备份搭车补：最新快照超 24 小时（或没有）才补一轮，与容器内 auto-maint 同一门禁
	@LATEST=$$(ls -t data/backups 2>/dev/null | head -1); \
	NEED=1; \
	if [ -n "$$LATEST" ]; then \
	  MT=$$(stat -f %m "data/backups/$$LATEST" 2>/dev/null || echo 0); \
	  NOW=$$(date +%s); [ $$((NOW-MT)) -lt 86400 ] && NEED=0; \
	fi; \
	if [ "$$NEED" = "1" ]; then echo "快照超 24h，自动补一轮备份…"; $(PY) tools/backup.py; fi

## 停止
picwall-stop:
	$(COMPOSE) down

## 跟踪日志
picwall-logs:
	$(COMPOSE) logs -f --tail 100 picwall

## 单测 + lint（宿主 .venv；改完代码先跑这条再 make picwall）
picwall-test:
	$(PY) -m pytest -q
	$(PY) -m ruff check app tools scripts tests
	@command -v node >/dev/null 2>&1 && node tests/frontend.test.mjs || echo "（无 node，跳过前端测试）"

## 临时演示数据的浏览器验收实例（18081 端口，Ctrl-C 退出自动销毁；永不碰真实 data/）
picwall-preview:
	$(PY) tools/preview_fixture.py

## 只读快照测查询（GROWTH=1 测 2 万帖模拟增长）
picwall-benchmark:
	GROWTH=$(GROWTH) $(PY) tools/benchmark.py

## 完整图片墙备份（db 快照 + 凭据留本地；blob 按 PICWALL_BLOB_STORE 落本地或夸克）
picwall-backup:
	$(PY) tools/backup.py

backup: picwall-backup

## 恢复演练：最新快照恢复到临时目录逐文件校验后清理；--target 目录 则真实恢复
picwall-restore-test:
	$(PY) tools/restore.py

## 深度体检原图/缩略图/头像/历史归档（体检「完成」≠ 历史缺失已补齐）
picwall-health:
	$(PY) tools/maintenance.py

## 只修复可重建内容，保留用户决定
picwall-repair:
	$(PY) tools/maintenance.py --repair

## 备份 blob 库同步夸克：可加 ARGS="--verify-sample 50 --prune-local"（首传/续传/回验/prune）
picwall-cloud:
	$(PY) tools/backup_to_quark.py $(CLOUD_ARGS)

## 签发设备配对码（10 分钟有效，仅可用一次；ROLE=admin/viewer 换角色）
picwall-pair:
	$(PY) tools/pair.py $(ROLE)

## 签发浏览器采集令牌（30 天，仅可导入）
picwall-collector-token:
	$(PY) tools/pair.py --collector-token

## 从本机 Safari 现取 x.com 登录态发给图片墙（终端需「完全磁盘访问」；默认只回显尾 4 位）
picwall-safari-cookie:
	$(PY) tools/safari_x_cookie.py $(ARGS)

## 媒体只读审计（体积/重复/库-磁盘一致性）
picwall-audit:
	$(PY) tools/audit.py

## 重打包 Cookie 助手扩展 zip（make picwall 前置自动执行；zip 内保留 extension/ 前缀，解压后直接加载该目录）
extension:
	@command -v zip >/dev/null 2>&1 || { echo "（无 zip 命令，跳过扩展重打包）"; exit 0; }
	mkdir -p app/static/extension
	zip -X -q -r app/static/extension/pwall-cookie-helper.zip extension -x 'extension/.DS_Store' 'extension/._*' 'extension/.*'

## 快照状态速览：容器状态 + 最新备份批次新鲜度（超 24h 提示补备份）
status:
	@$(COMPOSE) ps
	@LATEST=$$(ls -t data/backups 2>/dev/null | head -1); \
	if [ -z "$$LATEST" ]; then echo "备份：暂无快照（跑 make picwall-backup）"; else \
	  MT=$$(stat -f %m "data/backups/$$LATEST" 2>/dev/null || echo 0); \
	  H=$$(( ($$(date +%s)-MT) / 3600 )); \
	  if [ $$H -ge 24 ]; then echo "备份：$$LATEST（$$H 小时前，建议 make picwall-backup）"; \
	  else echo "备份：$$LATEST（$$H 小时前）"; fi; \
	fi

## 校验架构图谱与源码一致（行号 ±10 内）
atlas-verify:
	$(PY) scripts/verify_atlas.py
