from app.models.base import Base
from app.models.system import (REAL_NAME_MAX_LENGTH, SysUser, SystemSetting,
                             UserBook)
from app.models.book import (Book, BookYear, Period, PeriodOpening, book_first_ord,
                           ord_to_month, period_ord, year_start_ord)
from app.models.accounting import Entry, Subject, Voucher
from app.models.report import ReportCell, ReportCol, ReportResult, ReportRow, ReportTemplate
from app.models.printing import PrintTemplate
from app.models.importing import ImportBatch
from app.models.invoice import InvoiceBatch, InvoiceConfig, InvoiceRecord, InvoiceRule
from app.models.bankstatement import (DIRECTION_IN, DIRECTION_OUT,
                                     DIRECTION_UNKNOWN, PLAN_MANUAL, PLAN_NONE,
                                     PLAN_PENDING, PLAN_RULE, STATUS_IGNORED,
                                     STATUS_OPEN, STATUS_POSTED, STATUS_REVIEW,
                                     BankStatement,
                                     BankStatementConfig, BankStatementProfile,
                                     BankStatementRow, BankStatementRowSubject,
                                     BankStatementRule)
from app.models.audit import AuditLog
from app.models.mcp import McpToken
from app.models.backup import (BackupRun, KEEP_DAYS, SCOPE_BOOK, SCOPE_SYSTEM, STATUS_FAILED,
                          STATUS_SUCCESS, TIER_DAILY, TIER_MONTHLY, TIERS, TIER_WEEKLY)
from app.models.mfa import (MFA_STATUSES, STATUS_CONSUMED, STATUS_EXPIRED,
                            STATUS_PENDING, STATUS_VERIFIED, AdminMfaChallenge)
from app.models.idempotency import VoucherIdempotency

# **模型层文本守卫**（必须在这里 import，不能等到用得到的时候才 import）：
# 它靠 ``@event.listens_for`` 挂上 before_insert / before_update 钩子，
# 晚一步 import 就等于没挂 —— 而「没挂」正是这个模块存在的理由。
from app.models import text_guard  # noqa: F401

__all__ = [
    "Base", "SysUser", "UserBook", "SystemSetting",
    "Book", "BookYear", "Period", "PeriodOpening",
    "period_ord", "year_start_ord", "ord_to_month", "book_first_ord",
    "Subject", "Voucher", "Entry",
    "ReportTemplate", "ReportRow", "ReportCol", "ReportCell", "ReportResult",
    "PrintTemplate", "AuditLog", "ImportBatch", "McpToken",
    "InvoiceConfig", "InvoiceRule", "InvoiceBatch", "InvoiceRecord",
    "BankStatement", "BankStatementRow", "BankStatementRule", "BankStatementProfile",
    "BankStatementRowSubject", "BankStatementConfig",
    "DIRECTION_IN", "DIRECTION_OUT", "DIRECTION_UNKNOWN",
    "STATUS_OPEN", "STATUS_POSTED", "STATUS_IGNORED", "STATUS_REVIEW",
    "PLAN_NONE", "PLAN_RULE", "PLAN_MANUAL", "PLAN_PENDING",
    "BackupRun", "TIERS", "KEEP_DAYS",
    "TIER_DAILY", "TIER_WEEKLY", "TIER_MONTHLY",
    "STATUS_SUCCESS", "STATUS_FAILED", "SCOPE_BOOK", "SCOPE_SYSTEM",
    "AdminMfaChallenge", "MFA_STATUSES",
    "STATUS_PENDING", "STATUS_VERIFIED", "STATUS_CONSUMED", "STATUS_EXPIRED",
    "VoucherIdempotency",
]

# Import for schema discovery; runtime packages never join Base.metadata.
from app.models.plugin import PluginRegistry, PluginInstallAttempt, PluginUserAccess, PluginCohort, PluginWorkerRuntime

from app.models.plugin_import import PluginImportBatch, PluginImportFile, PluginVoucherOrigin
from app.models.scheduler import ScheduledTask, ScheduledTaskRun

__all__ += ["ScheduledTask", "ScheduledTaskRun"]
