"""令牌版本号：让「重置口令 / 停用账号」立刻作废已签发的 JWT（审计 P2-01）

Revision ID: 0004_token_version
Revises: 0003_converge_legacy
Create Date: 2026-10-13

**这个漏洞的形状：改完口令，令牌照样好使。**

JWT 是自签名的，服务端不存会话，所以「验证一个令牌」的全部依据只有三样：
签名对不对、有没有过期、用户还 active 吗。管理员重置某人的口令时
``update_user`` 只改了 ``password_hash`` —— 已经发出去的那些 bearer token
一个都没作废。于是口令泄露之后的标准处置（改密）实际上没起作用：攻击者
手里的旧令牌最长还能用满 720 分钟（普通用户 12 小时），期间照常读账、写凭证。

停用账号能挡住（``active`` 会在鉴权时被查），但「改密」这条最常用的处置路径
是漏的 —— 而且它恰恰是唯一不需要管理员权衡的处置方式。

**做法是一个整数。** ``token_version`` 签发时进载荷（``tv``），鉴权时与库里
比一次，不等就拒。撤销 = ``token_version += 1``，该账号所有在用令牌一次性作废，
代价是这个人重新登录一次。

选它而不是别的方案的理由：

* 不引入服务端会话表 —— 那意味着每个请求一次数据库写入，而这里
  ``get_current_user`` 本来就要 ``db.get(SysUser, ...)``，顺带比一个整数，
  **不增加任何查询**。
* 不靠「口令哈希前 8 位塞进载荷」这类隐式绑定 —— 改一次口令碰巧让所有
  旧令牌失效的原因是散列盐变了，而这是巧合不是机制：谁哪天把
  ``hash_password`` 换成不带随机盐的实现，整条撤销路径静默失效。
* 放账号行而不是进程内存：多进程部署下每个进程各有一份内存状态，
  撤销只撤销了其中一份；重启即清零，等于没实现。
  （这与登录失败锁定选择放账号上是同一个理由，见 ``app/models/system.py``。）

**存量行取 0，不是 NULL。** server_default 先给存量行兜底，随后撤掉 ——
应用侧是显式写 0 的，这层默认留着只会让将来加默认值的人少看见一次变更。
升级前签发的令牌载荷里没有 ``tv``，鉴权按 0 处理，仍然可用；
一旦发生改密或停用，它们立即失效，这正是期望行为。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_token_version"
down_revision = "0003_converge_legacy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sys_user", sa.Column(
        "token_version", sa.Integer(), nullable=False, server_default="0"))
    op.alter_column("sys_user", "token_version", server_default=None)


def downgrade() -> None:
    # 撤掉这一列 = 撤销能力本身没了：口令改了旧令牌又会一直有效。
    # 这属于有损降级，和 0003 那条把外键改回 CASCADE 同性质 ——
    # 该做的是回到代码对应的版本，而不是 downgrade。
    op.drop_column("sys_user", "token_version")
