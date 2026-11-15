"""把本地备份 blob 库同步到夸克网盘（首传/续传），可选抽样校验与本地清理。

用法（宿主侧，仓库根目录 make picwall-cloud 或直接跑）：
  python tools/backup_to_quark.py                     # 只传缺的，幂等可续跑
  python tools/backup_to_quark.py --verify-sample 50  # 传完后随机抽样回读校验
  python tools/backup_to_quark.py --prune-local       # 覆盖核对通过后删除本地 blobs/
                                                       # （必须配合 --verify-sample，
                                                       #  且默认只删「云端已登记」的文件）

本地 blobs 是 pics/export 的派生数据（内容寻址镜像），误删也可由 local 模式
backup.create() 重建；但清理仍走「全量覆盖核对 + 抽样回读 sha256」双门禁。
"""
import argparse
import hashlib
import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import cloud, config
from tools import runtime


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def local_blobs() -> list[Path]:
    blob_dir = config.DATA_DIR / "backups" / "blobs"
    if not blob_dir.is_dir():
        return []
    root = blob_dir.resolve()
    return sorted(
        p for p in blob_dir.iterdir()
        if not p.is_symlink() and p.is_file() and not p.name.startswith(".")
        and p.resolve().is_relative_to(root)
    )


def sample_verify(store: cloud.QuarkBlobs, n: int, seed: int = 0) -> list[str]:
    """随机抽 n 个已登记 blob 从云端回读，sha256 不符/读取失败即进返回的错误清单。"""
    del store
    rows = runtime.cloud_db("list_blobs") or []
    if not rows:
        return ["云端登记表为空，无法校验"]
    picked = random.Random(seed).sample(rows, min(n, len(rows)))
    errors = []
    with tempfile.TemporaryDirectory(prefix="quark-verify-") as tmp:
        for row in picked:
            ok = "✓"
            try:
                src = cloud.read_blob(row["fid"], Path(tmp))
                if digest(Path(src)) != row["key"]:
                    errors.append(f"{row['key'][:12]}… 回读 sha256 不符")
                    ok = "✗"
            except cloud.BlobError as e:
                errors.append(f"{row['key'][:12]}… {e}")
                ok = "✗"
            print(f"  校验 {row['key'][:12]}… {ok}", flush=True)
    return errors


def prune_local(known: set[str], prune_all: bool = False) -> int:
    """删除本地 blobs/ 中云端已登记的文件（prune_all=True 时连同未登记文件一并清理）；目录空了连目录一起删。"""
    blob_dir = config.DATA_DIR / "backups" / "blobs"
    if not blob_dir.is_dir():
        return 0
    root = blob_dir.resolve()
    removed = 0
    for p in local_blobs():
        if not p.resolve().is_relative_to(root):
            continue
        if prune_all or p.name in known:
            p.unlink()
            removed += 1
    if blob_dir.is_dir() and not any(blob_dir.iterdir()):
        blob_dir.rmdir()
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=0, help="本次最多上传多少个（0=不限，冒烟用）")
    ap.add_argument("--verify-sample", type=int, default=0, help="随机抽样回读校验条数")
    ap.add_argument("--prune-local", action="store_true",
                    help="校验通过后删除本地 blobs/（含未登记文件需 --prune-all）")
    ap.add_argument("--prune-all", action="store_true",
                    help="prune 时连同云端未登记的本地文件一起删（默认保留并警告）")
    ap.add_argument("--seed", type=int, default=0, help="抽样随机种子（0=时间）")
    args = ap.parse_args()

    if config.BLOB_STORE != "quark":
        print("提示：PICWALL_BLOB_STORE!=quark，本工具仍可跑（同步不依赖该开关）", flush=True)

    runtime.cloud_db("init_db")   # 幂等：容器活时经 docker exec 在同内核登记
    local = local_blobs()
    print(f"本地 blobs：{len(local)} 个", flush=True)
    store = cloud.QuarkBlobs()
    known = set(runtime.cloud_db("known_keys") or ())
    print(f"云端已登记：{len(known)} 个", flush=True)

    pending = [(p.name, p) for p in local if p.name not in known]
    if args.limit:
        pending = pending[:args.limit]
    print(f"待上传：{len(pending)} 个（{sum(p.stat().st_size for _, p in pending) / 1e9:.2f} GB）", flush=True)

    uploaded = failed = 0
    batch = store.batch
    for i in range(0, len(pending), batch):
        chunk = pending[i:i + batch]
        try:
            result = cloud.upload_files([p for _, p in chunk], store.dir_fid())
        except cloud.BlobError as e:
            print(f"批次失败（{i}–{i + len(chunk)}）：{e}", flush=True)
            failed += len(chunk)
            continue
        by_name = {p.name: (sha, p) for sha, p in chunk}
        reg_items = [
            {"key": by_name[name][0], "fid": fid, "size": by_name[name][1].stat().st_size}
            for name, fid in result.items()
        ]
        if reg_items:
            runtime.cloud_db("register", {"items": reg_items})
        uploaded += len(result)
        done = min(i + batch, len(pending))
        print(f"  进度 {done}/{len(pending)}（+{len(result)}）", flush=True)
    if failed:
        print(f"完成：上传 {uploaded}，失败 {failed}（重跑本命令续传）", flush=True)
        return 1
    print(f"上传完成：{uploaded} 个，全部登记", flush=True)

    known = set(runtime.cloud_db("known_keys") or ())
    if args.verify_sample:
        errors = sample_verify(store, args.verify_sample,
                               seed=args.seed or int(datetime.now().timestamp()))
        if errors:
            print("抽样校验失败：\n  " + "\n  ".join(errors[:10]), flush=True)
            return 1
        print(f"抽样校验通过（{args.verify_sample} 个）", flush=True)

    if args.prune_local:
        if not args.verify_sample:
            print("拒绝 prune：必须先 --verify-sample 抽样校验", flush=True)
            return 1
        unregistered = [p.name for p in local if p.name not in known]
        if unregistered and not args.prune_all:
            print(f"本地仍有 {len(unregistered)} 个未登记 blob，默认保留"
                  f"（确认云端不需要它们时加 --prune-all）", flush=True)
        removed = prune_local(known, prune_all=args.prune_all)
        left = len(local_blobs())
        print(f"本地清理：删除 {removed} 个，剩余 {left} 个"
              + ("（未登记保留）" if unregistered and not args.prune_all else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
