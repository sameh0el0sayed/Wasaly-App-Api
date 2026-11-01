"""Arabic <-> English for server-generated texts (notifications, error messages, notes).

The code base writes the Arabic text (default language). `localize(text, "en")` returns the English version:
exact matches first, then templates with {placeholders} (their captured parts are translated again, so
"... السبب: <user text>" works and user-written text passes through untouched). Unknown text is returned as is.
To add a language, add another column here and a code to LANGUAGES.
"""
import re

LANGUAGES = ("ar", "en")
DEFAULT_LANGUAGE = "ar"

# (arabic, english). Placeholders: {name}. Order does not matter: every entry is anchored (^...$).
PAIRS: list[tuple[str, str]] = [
    # account / auth
    ("لا يمكن حذف الحساب أثناء وجود طلب جارٍ", "You cannot delete your account while an order is in progress"),
    ("يوجد مبلغ غير مدفوع", "You have an unpaid amount"),
    ("يوجد اشتراك ساري", "You have an active subscription"),
    ("يوجد مبلغ مستحق للشركة", "You owe an outstanding amount to the company"),
    ("تم حذف حسابك", "Your account has been deleted"),
    ("حسابك قيد المراجعة من الإدارة", "Your account is under review"),
    ("تم تحديث بياناتك", "Your details were updated"),
    ("مطلوب تصحيح بيانات", "Data correction required"),
    ("يرجى مراجعة البيانات المطلوب تصحيحها وإعادة إرسالها.", "Please review the data that needs correcting and send it again."),
    ("تم إرسال تصحيح بيانات", "A data correction was submitted"), ("أرسل مستخدم تصحيحًا للمراجعة.", "A user submitted a correction for review."),
    ("طلب تغيير بيانات", "Data change request"), ("طلب مستخدم تغيير رقم هاتفه.", "A user asked to change their phone number."),
    ("تم قبول التصحيح", "Correction accepted"), ("تم قبول البيانات المرسلة.", "The submitted data was accepted."),
    ("تم رفض التصحيح", "Correction rejected"), ("يرجى إعادة إرسال البيانات.", "Please send the data again."),
    ("تم قبول طلب التغيير", "Change request accepted"),
    ("تم رفض طلب التغيير", "Change request rejected"), ("لم تتم الموافقة على التغيير.", "The change was not approved."),
    ("قامت الإدارة بتحديث بياناتك.", "The administration updated your details."),
    ("هذا الحساب محذوف", "This account has been deleted"),
    ("إذا كان الحساب موجودًا فسيتواصل معك المدير لإعادة تعيين كلمة المرور",
     "If the account exists, an administrator will contact you to reset your password"),
    ("تم إرسال الطلب للمراجعة", "Your request was sent for review"),
    ("تم إرسال طلبك للإدارة", "Your request was sent to the administration"),
    ("تمت الموافقة", "Approved"), ("تمت الموافقة على حسابك.", "Your account has been approved."),
    ("تم رفض الحساب", "Account rejected"), ("تم رفض حسابك.", "Your account has been rejected."),
    ("مطلوب تعديل البيانات", "Changes required"), ("يرجى تعديل بياناتك.", "Please update your details."),
    ("تم إيقاف الحساب", "Account suspended"), ("تم إيقاف حسابك.", "Your account has been suspended."),
    ("تمت إعادة التفعيل", "Account reactivated"), ("تمت إعادة تفعيل حسابك.", "Your account has been reactivated."),
    ("تمت إعادة تفعيل حسابك", "Your account has been reactivated"),
    ("تمت إعادة تفعيل حسابك بعد سداد الرسوم.", "Your account has been reactivated after the fee was paid."),
    ("{msg} السبب: {reason}", "{msg} Reason: {reason}"),
    ("مراجعة مستند", "Document review"), ("رد الإدارة", "Reply from administration"),
    ("تم حظر حسابك نهائيًا", "Your account has been permanently banned"),
    ("تم إيقاف حسابك مؤقتًا", "Your account has been temporarily suspended"),
    ("بسبب تقييمات منخفضة متكررة.", "Because of repeated low ratings."),
    ("بسبب تقييم منخفض. تواصل مع الإدارة لمراجعة حسابك.", "Because of a low rating. Contact support to review your account."),
    ("إيقاف تلقائي بسبب التقييم", "Automatic suspension due to rating"),
    ("{name} (#{id}) وصل لحد التقييمات بنجمة واحدة", "{name} (#{id}) reached the one-star rating limit"),
    ("مستند منتهي", "Expired document"),
    ("تم إيقاف عملك حتى تجديد المستند واعتماده.", "Your work is suspended until the document is renewed and approved."),
    ("تنبيه انتهاء مستند", "Document expiry notice"), ("ستنتهي صلاحية: {docs}", "Will expire: {docs}"),
    # orders
    ("تم استلام طلبك", "Your order was received"), ("نبحث لك عن سائق.", "We are looking for a driver."),
    ("تم قبول طلبك", "Your order was accepted"), ("السائق {name} - {car}", "Driver {name} - {car}"),
    ("السائق في الطريق", "Driver on the way"), ("السائق في طريقه إلى نقطة الالتقاء.", "The driver is heading to the pickup point."),
    ("وصل السائق", "Driver arrived"), ("السائق وصل إلى نقطة الالتقاء.", "The driver has reached the pickup point."),
    ("تم استلام الطلب", "Order picked up"), ("استلم السائق طلبك وبدأت الرحلة.", "The driver picked up your order and the trip has started."),
    ("بدأت الرحلة", "Trip started"), ("بدأت رحلتك.", "Your trip has started."),
    ("تم التسليم", "Delivered"), ("تم تسليم الطلب. المبلغ المطلوب: {amt}", "Order delivered. Amount due: {amt}"),
    ("اكتملت الرحلة", "Trip completed"), ("المبلغ المطلوب: {amt}", "Amount due: {amt}"),
    ("أرباحك من الرحلة: {amt}", "Your earnings from the trip: {amt}"),
    ("تم إلغاء الطلب", "Order cancelled"), ("ألغى العميل الطلب.", "The customer cancelled the order."),
    ("ألغى السائق الطلب: {reason}", "The driver cancelled the order: {reason}"),
    ("ألغت الإدارة الطلب: {reason}", "The administration cancelled the order: {reason}"),
    ("لا يوجد سائق متاح", "No driver available"),
    ("لم نجد سائقًا حتى الآن، يمكنك إعادة المحاولة أو إلغاء الطلب.", "We have not found a driver yet. You can retry or cancel the order."),
    ("لم يقبل أي سائق الطلب حتى الآن. يمكنك إعادة المحاولة.", "No driver has accepted the order yet. You can retry."),
    ("انتهت صلاحية الطلب", "Order expired"), ("انتهت مهلة الطلب دون إسناده لسائق.", "The order timed out without a driver."),
    ("طلب جديد", "New order"), ("طلب جديد من {place}", "New order from {place}"),
    ("انتهت مهلة العرض", "Offer expired"), ("لم يتم قبول الطلب خلال {n} ثوانٍ.", "The order was not accepted within {n} seconds."),
    ("الخدمة متوقفة مؤقتًا", "Service is temporarily unavailable"),
    ("يرجى سداد الرحلة السابقة أولًا", "Please pay for your previous trip first"),
    ("نقطة الالتقاء خارج مناطق الخدمة", "The pickup point is outside the service areas"),
    ("لا يمكن الإلغاء بعد بدء الرحلة، تواصل مع الإدارة", "The order cannot be cancelled after the trip has started; contact support"),
    ("تم الوصول للحد الأقصى لإعادة المحاولة، يرجى إنشاء طلب جديد", "Retry limit reached, please create a new order"),
    ("تقديري: السعر النهائي يُحسب من وقت الرحلة الفعلي من بدء الرحلة حتى الوصول.",
     "Estimate: the final price is calculated from the actual trip time, from trip start until arrival."),
    # payments / payouts
    ("تم تأكيد الدفع", "Payment confirmed"), ("تم استلام {amt}", "Received {amt}"),
    ("تم رفض إيصال الدفع", "Payment receipt rejected"),
    ("إيصال BenefitPay جديد", "New BenefitPay receipt"), ("دفعة #{id} بانتظار التأكيد", "Payment #{id} awaiting confirmation"),
    ("دفعة Tap غير مطابقة", "Tap payment mismatch"),
    ("خصم من أرباحك", "Deduction from your earnings"),
    ("تم خصم {off} من أرباحك لسداد مبلغ مستحق للشركة. المتبقي: {rem}",
     "{off} was deducted from your earnings to settle an amount owed to the company. Remaining: {rem}"),
    ("تم تحويل أرباحك", "Your earnings were transferred"), ("{amt} (تجريبي)", "{amt} (test)"),
    ("أضف حساب التحويل", "Add your payout account"), ("أضف رقم الحساب لاستلام أرباحك.", "Add your account number to receive your earnings."),
    ("تحويل سائق معلّق", "Driver payout on hold"), ("السائق {name}: لا يوجد حساب تحويل", "Driver {name}: no payout account"),
    ("فشل تحويل سائق", "Driver payout failed"),
    # promo
    ("أكواد الخصم غير متاحة حاليًا", "Promo codes are not available right now"),
    ("كود الخصم غير صحيح", "Invalid promo code"), ("كود الخصم لم يبدأ بعد", "This promo code is not active yet"),
    ("انتهت صلاحية كود الخصم", "This promo code has expired"),
    ("الحد الأدنى للطلب لاستخدام الكود {min}", "Minimum order to use this code is {min}"),
    # subscriptions
    ("طلب اشتراك جديد", "New subscription request"), ("اشتراك #{id} ({cat})", "Subscription #{id} ({cat})"),
    ("إلغاء اشتراك", "Subscription cancelled"), ("تم إلغاء اشتراك", "Subscription cancelled"),
    ("اشتراك #{id}: {reason}", "Subscription #{id}: {reason}"), ("اشتراك #{id}", "Subscription #{id}"),
    ("تم رفض طلب الاشتراك", "Subscription request rejected"), ("اشتراك شهري جديد", "New monthly subscription"),
    ("اشتراك #{id} حتى {d}", "Subscription #{id} until {d}"),
    ("تم تسعير اشتراكك", "Your subscription was priced"), ("{amt} / شهر — يرجى السداد للتفعيل", "{amt} / month — please pay to activate"),
    ("تم تفعيل اشتراكك", "Your subscription is active"), ("ساري حتى {d}", "Valid until {d}"),
    ("انتهى اشتراكك", "Your subscription has ended"), ("جدّد الاشتراك لمتابعة الرحلات", "Renew your subscription to continue your trips"),
    ("اشتراكك ينتهي قريبًا", "Your subscription ends soon"), ("ينتهي في {d}", "Ends on {d}"),
]

_EXACT: dict[str, str] = {}
_TEMPLATES: list[tuple[re.Pattern, str]] = []
for _ar, _en in PAIRS:
    if "{" in _ar:
        parts = re.split(r"\{(\w+)\}", _ar)  # literal, name, literal, name, ...
        rx = "".join(re.escape(p) if i % 2 == 0 else f"(?P<{p}>.+?)" for i, p in enumerate(parts))
        _TEMPLATES.append((re.compile(f"^{rx}$", re.S), _en))
    else:
        _EXACT[_ar] = _en
_TEMPLATES.sort(key=lambda t: -len(t[0].pattern))  # most specific (longest) first


def to_en(text: str, _depth: int = 0) -> str:
    if text in _EXACT:
        return _EXACT[text]
    if _depth < 3:
        for rx, en in _TEMPLATES:
            m = rx.match(text)
            if m:
                return en.format(**{k: to_en(v, _depth + 1) for k, v in m.groupdict().items()})
    return text


def localize(text, lang: str):
    """Arabic is the source language: `ar` returns the text unchanged."""
    if lang != "en" or not isinstance(text, str):
        return text
    return to_en(text)


def localize_detail(detail, lang: str):
    """Error `detail`: a string, or a dict whose `message`/`note` is translated (codes never change)."""
    if lang != "en":
        return detail
    if isinstance(detail, str):
        return to_en(detail)
    if isinstance(detail, dict):
        return {k: (to_en(v) if k in ("message", "note") and isinstance(v, str) else v) for k, v in detail.items()}
    return detail


def parse_accept_language(header: str | None) -> str | None:
    """'en-US,en;q=0.9,ar;q=0.8' -> 'en'. None when the header is missing or names no supported language."""
    for part in (header or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGUAGES:
            return code
    return None
