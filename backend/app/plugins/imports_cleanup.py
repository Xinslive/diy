"""到期插件导入内容清理：默认只统计，--apply 才修改。"""
import argparse
from sqlalchemy import func, select
from app.core.db import SessionLocal
from app.models.plugin_import import PluginImportBatch, PluginImportFile
from app.plugins.imports import now, purge_expired


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="清除到期内容；保留摘要、提交结果和来源关联")
    args = parser.parse_args()
    with SessionLocal.begin() as db:
        if args.apply:
            print(f"已清除 {purge_expired(db)} 份到期源文件；摘要和提交结果保留")
        else:
            count = db.scalar(select(func.count()).select_from(PluginImportFile).join(PluginImportBatch).where(
                PluginImportBatch.expires_at <= now(), PluginImportFile.content.is_not(None)))
            print(f"{count} 份源文件到期；使用 --apply 执行清理")


if __name__ == "__main__":
    main()
