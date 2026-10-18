# syntax=docker/dockerfile:1
#
# ============================================================================
# 定位：**开发/试验用**的单容器镜像，不是生产部署方案。
# ============================================================================
# 原来这个文件旁边放着一个 docker-compose.yml，头注释写着「生产环境单容器部署：
# Postgres + API + 前端静态文件」。那是假的：
#   * compose 里只有 db + api 两个 service，没有 nginx、没有前端容器，
#     却宣称「+ 前端静态文件」；
#   * 全仓没有任何一处引用它（README、deploy/、dev.sh 都不提）；
#   * 真实开发路径是 `docker run -d --name cwq-pg -p 15432:5432 postgres:16`
#     （见 README 与 dev.sh），容器名 cwq-pg，端口 15432 ——
#     和 compose 里的 cwq-db 不是同一个东西，也不对外暴露。
#   * 真实生产路径是 deploy/：systemd 单元 + nginx + /opt/cwq。
# 所以 compose 已删除（P1-24）。这个镜像保留下来，作为「拿 Docker 试一下」的
# 通道，由 CI 构建并检查迁移文件；生产职责仍由 deploy/ 承担。
#
# 生产部署请用 deploy/deploy.sh（备份 → 装依赖 → 跑测试 → 构建前端 → 迁移 →
# 切产物 → 重启 → 健康检查）。
#
# ---------------------------------------------------------------------------
# 这里原来必然启动失败（P2-32）
# ---------------------------------------------------------------------------
# 旧 Dockerfile 只 COPY 了 app/ 和 scripts/。而 app/core/schema_version.py 用
# BACKEND_ROOT = parents[2] 去找 alembic.ini 和 migrations/，在容器里就是
# /app/alembic.ini 与 /app/migrations —— 两个都不存在 → head_revision() 返回
# None → assert_schema_ready() 抛「部署不完整（migrations 目录没跟着代码走）」
# → gunicorn 每个 worker 一启动就退出。也就是说这条路径**从来没被打通过**。
# 下面把 alembic.ini 与 migrations/ 一起复制进来，并在启动前跑一次
# `alembic upgrade head`，让这条路径至少是自洽可用的。
#
# 注意这个修复**不依赖** migrations/env.py 的任何改动：0006 那条 revision 刻意
# 没用 CREATE INDEX CONCURRENTLY（文件头写明了原因），所以在当前 env.py 的
# 「整条 revision 链一个事务」语义下 upgrade 能正常跑完。
# ============================================================================

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 tzdata \
    && rm -rf /var/lib/apt/lists/*

# 装**锁文件**，不是 requirements.txt。
# requirements.txt 全是「>=」，用它装镜像的话，构建结果取决于构建那天 PyPI 上
# 是什么版本 —— 同一个 Dockerfile 今天和下个月构建出来的东西不是一回事，
# 「镜像能跑」和「测试当时是绿的」之间也就没有任何版本等价关系了。
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY app ./app
COPY scripts ./scripts

# 启动闸门（app/core/schema_version.py）要读这两样，少一样就拒绝启动
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

# ------------------------------------------------------------------ 非 root
#
# 原来整个容器以 root 跑。应用只需要读自己的代码目录，而 CMD 里那条
# ``alembic upgrade head`` 写的是**数据库**、不是文件系统 —— 所以降到非 root
# 不会让任何一步跑不起来。systemd 那边本来就是 ``User=cwq``，这条让容器
# 与之对齐：同一个应用在两条部署路径上的权限姿态不再是两回事。
#
# uid/gid 固定成 10001，是为了 bind mount 宿主机目录时对得上，而不是随构建
# 漂移。nologin + 无 home：这个进程不登录、不需要家目录。
RUN groupadd --gid 10001 cwq \
 && useradd --uid 10001 --gid cwq --no-create-home \
            --home-dir /nonexistent --shell /usr/sbin/nologin cwq \
 && mkdir -p /app/data/plugins /app/logs \
 && chown -R cwq:cwq /app/data /app/logs
VOLUME ["/app/data"]
USER cwq

EXPOSE 8000

# 启动前先迁移，是应用自己的硬性要求，不是顺手加的好习惯：
# 应用**只**校验 schema 版本、不执行任何 DDL（见 schema_version.py 的模块说明），
# 版本对不上就拒绝启动。所以容器必须先 ``upgrade head``，否则每个 worker 启动即退出。
# 用 && 而不是 ;：迁移失败就让容器以非零退出（响亮地失败），绝不能让它跳过迁移
# 继续起服务 —— 那等于带着旧 schema 跑起来，问题要到运行时才炸。
# 并发边界（明说）：两个容器同时起会同时跑 upgrade。这里是开发/试验通道，
# 不做迁移锁；要并发部署请走 deploy/，那里由 systemd 串行化。
#
# worker 数：默认 2，与 deploy/systemd/cwq-api.service 的默认值一致。
# 这里**能**用 ${VAR:-默认} —— 因为 CMD 走的是 `sh -c`，有 shell 做展开。
# （systemd 那边没有 shell，用的是另一套写法，见那个 unit 里的注释。）
#
# ``--threads`` 已删除：gunicorn 的 thread 开关只对**同步** worker 生效，而这里
# 是 ``-k uvicorn.workers.UvicornWorker``（异步 worker），每个进程已经是事件循环，
# 起线程没有作用。它此前在这里静默无效，容易让人以为「4 线程」真的存在。
CMD ["sh", "-c", "alembic upgrade head && exec python scripts/start_server.py --server gunicorn --host 0.0.0.0 --port 8000"]
