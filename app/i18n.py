"""Arabic <-> English for server-generated texts (notifications, error messages, notes).

The code base writes the Arabic text (default language). `localize(text, "en")` returns the English version:
exact matches first, then templates with {placeholders} (their captured parts are translated again, so
"... السبب: <user text>" works and user-written text passes through untouched). Unknown text is returned as is.
To add a language, add another column here and a code to LANGUAGES.
"""
import re

LANGUAGES = ("ar", "en")
DEFAULT_LANGUAGE = "en"

# (arabic, english). Placeholders: {name}. Order does not matter: every entry is anchored (^...$).
PAIRS: list[tuple[str, str]] = [
    # v20 wallet
    ("تم إيداع رصيد في محفظتك", "Funds were added to your wallet"), ("أُضيف {amt} إلى محفظتك.", "{amt} was added to your wallet."),
    ("تم خصم رصيد من محفظتك", "Funds were deducted from your wallet"), ("خُصم {amt} من محفظتك.", "{amt} was deducted from your wallet."),
    # v16 passkeys
    ("أُضيف مفتاح مرور جديد إلى حسابك", "A new passkey was added to your account"),
    ("مفتاح مرور جديد", "New passkey"),
    # v15 role switch
    ("طلب انضمام سائق", "Driver application"),
    ("قدّم {name} طلبًا ليصبح سائقًا.", "{name} applied to become a driver."),
    ("تم اعتماد طلبك", "Your application was approved"),
    ("تم اعتماد حسابك كسائق. يمكنك التبديل إلى وضع السائق من الإعدادات.", "Your driver account is approved. You can switch to driver mode in Settings."),
    ("تم رفض طلب الانضمام كسائق", "Your driver application was rejected"),
    ("لم يتم اعتماد طلبك. السبب: {r}", "Your application was not approved. Reason: {r}"),
    ("مطلوب تعديل على طلب السائق", "Your driver application needs changes"),
    ("يرجى تعديل بيانات الطلب وإعادة إرساله. السبب: {r}", "Please update your application and resubmit it. Reason: {r}"),
    # v14 licence
    ("تنتهي رخصة القيادة بعد {n} يوم. حدّثها لتستمر في استقبال الطلبات.", "Your driving licence expires in {n} days. Renew it to keep receiving orders."),
    ("لم يتم اعتماد طلب تجديد الرخصة. السبب: {r}", "Your licence renewal was not approved. Reason: {r}"),
    ("انتهت رخصة السائق {name}.", "The licence of driver {name} has expired."),
    ("قدّم السائق {name} طلب تجديد الرخصة.", "Driver {name} submitted a licence renewal request."),
    ("رخصتك تنتهي قريبًا", "Your licence expires soon"),
    ("انتهت رخصتك", "Your licence has expired"),
    ("انتهت رخصة القيادة، وتوقف إرسال الطلبات إليك حتى تحديثها.", "Your driving licence has expired. Orders will not be sent to you until you renew it."),
    ("تم تجديد الرخصة", "Licence renewed"),
    ("تم اعتماد رخصتك الجديدة، ويمكنك استقبال الطلبات.", "Your new licence has been approved. You can receive orders again."),
    ("تم رفض تجديد الرخصة", "Licence renewal rejected"),
    ("رخصة سائق منتهية", "Driver licence expired"),
    ("طلب تجديد رخصة", "Licence renewal request"),
    # account / auth
    ("لا يمكن حذف الحساب أثناء وجود طلب جارٍ", "You cannot delete your account while an order is in progress"),
    ("يوجد مبلغ غير مدفوع", "You have an unpaid amount"),
    ("يوجد اشتراك ساري", "You have an active subscription"),
    ("يوجد مبلغ مستحق للشركة", "You owe an outstanding amount to the company"),
    ("تم حذف حسابك", "Your account has been deleted"),
    ("حسابك قيد المراجعة من الإدارة", "Your account is under review"),
    ("تم تحديث بياناتك", "Your details were updated"),
    ("سائق جاهز لطلبك", "A driver is ready for your request"), ("{name} قبل طلبك بسعر {price}", "{name} accepted your request for {price}"),
    ("سائق يطلب سعرًا أعلى", "A driver asks for a higher price"), ("{name} يطلب {price}", "{name} asks for {price}"),
    ("تم رفض طلب السعر", "Price request rejected"), ("لم يوافق العميل على السعر الأعلى.", "The customer did not accept the higher price."),
    ("انتهت مهلة طلب السعر", "Price request expired"), ("لم يرد العميل على طلب السعر في الوقت المحدد.", "The customer did not answer the price request in time."),
    ("تم قبول السعر الجديد", "New price accepted"), ("وافق العميل على السعر الجديد، توجّه للعميل.", "The customer accepted the new price. Head to the customer."),
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

# v52: English source strings (English is the default language); Arabic users get the Arabic text back
PAIRS += [
    ("الخدمة متوقفة مؤقتًا للصيانة", "The service is temporarily down for maintenance"),
    ("لا يمكن حذف الحساب أثناء وجود طلب جارٍ", "The account cannot be deleted while an order is in progress"),
    ("يوجد مبلغ غير مدفوع", "There is an unpaid amount"),
    ("يوجد اشتراك ساري", "There is an active subscription"),
    ("يوجد مبلغ مستحق للشركة", "There is an amount owed to the company"),
    ("تم حذف حسابك", "Your account was deleted"),
    ("تم تحديث بياناتك.", "Your details were updated."),
    ("تم التحقق من رقم الهاتف. حسابك الآن قيد المراجعة من الإدارة", "Phone number verified. Your account is now under admin review"),
    ("هذا الحساب محذوف", "This account has been deleted"),
    ("تم حظر هذا الحساب نهائيًا", "This account has been permanently banned"),
    ("إذا كان الحساب موجودًا فسيتواصل معك المدير لإعادة تعيين كلمة المرور", "If the account exists, the admin will contact you to reset the password"),
    ("تم بلوغ الحد الشهري للحساب المؤسسي", "The corporate account monthly limit has been reached"),
    ("المستخدم غير مسجل في وصلي", "This user is not registered in Wasaly"),
    ("تم إرسال الطلب للمراجعة", "The request was sent for review"),
    ("غرامة إلغاء", "Cancellation penalty"),
    ("السائق اعتذر", "Driver apologised"),
    ("اعتذر السائق، نبحث لك عن سائق آخر.", "The driver apologised; we are finding you another driver."),
    ("الطلب جاهز", "Order is ready"),
    ("الطلب جاهز للاستلام الآن.", "The order is ready for pickup now."),
    ("ألغت الجهة الطالبة الطلب.", "The requesting business cancelled the order."),
    ("يُحسب الوقت من «بدء الرحلة» حتى «الوصول للوجهة» فقط.", "Time is counted only from trip start to arrival at the destination."),
    ("تقديري: السعر النهائي يُحسب من وقت الرحلة الفعلي من بدء الرحلة حتى الوصول.", "Estimate: the final price is calculated from the actual trip time, from trip start to arrival."),
    ("الخدمة متوقفة مؤقتًا", "The service is temporarily unavailable"),
    ("يرجى سداد الرحلة السابقة أولًا", "Please pay for the previous trip first"),
    ("نقطة الالتقاء خارج مناطق الخدمة", "The pickup point is outside the service areas"),
    ("تم تأكيد رحلتك المجدولة", "Your scheduled trip is confirmed"),
    ("سنبحث لك عن سائق قبل الموعد.", "We will find you a driver before the scheduled time."),
    ("لا يمكن الإلغاء بعد بدء الرحلة، تواصل مع الإدارة", "Cancellation is not possible after the trip has started; contact support"),
    ("تعويض إلغاء", "Cancellation compensation"),
    ("تم احتساب تعويض لك عن إلغاء العميل.", "Compensation was credited to you for the customer's cancellation."),
    ("تم الوصول للحد الأقصى لإعادة المحاولة، يرجى إنشاء طلب جديد", "The retry limit was reached; please create a new order"),
    ("نوع بطاقة مختلف عن المختار", "Card type differs from the one selected"),
    ("تم الدفع بنجاح ✔", "Payment successful ✔"),
    ("فشلت عملية الدفع", "The payment failed"),
    ("جارٍ التحقق من الدفع…", "Verifying the payment..."),
    ("تم إرسال طلبك للإدارة", "Your request was sent to the admin"),
    ("طلب عرض سعر جديد", "New price quote request"),
    ("السعر المعروض هو السعر النهائي المتفق عليه عند التأكيد. أي تغيير يحتاج موافقتك.", "The price shown is the final agreed price at confirmation. Any change needs your approval."),
    ("نبحث لك عن مزوّد مناسب.", "We are finding a suitable provider for you."),
    ("طلب اعتماد مزوّد", "Provider approval request"),
    ("تم اعتماد خدمتك", "Your service was approved"),
    ("تم رفض طلب الخدمة", "Your service request was rejected"),
    ("عرض السعر جاهز", "Your price quote is ready"),
    ("طلب جديد لك", "New request for you"),
    ("أرسلت لك الإدارة طلبًا مباشرًا.", "The admin sent you a direct request."),
    ("تنبيه طوارئ", "Emergency alert"),
    ("تصعيد طوارئ", "Emergency escalation"),
    ("تم التعامل مع بلاغ الطوارئ", "The emergency report was handled"),
    ("تمت إضافة محطة", "A stop was added"),
    ("مكافأة إحالة", "Referral reward"),
    ("تحميل تطبيق Android", "Download the Android app"),
    ("تحميل تطبيق iPhone", "Download the iPhone app"),
    ("مستخدم محذوف", "Deleted user"),
    ("نبحث لك عن سائق", "We are finding you a driver"),
    ("حان موعد رحلتك المجدولة، نبحث عن سائق الآن.", "It is time for your scheduled trip; we are finding a driver now."),
    ("تذكير برحلتك", "Trip reminder"),
    ("رحلتك المجدولة بعد أقل من ساعة.", "Your scheduled trip starts in less than an hour."),
    ("انتهت المهلة", "Timed out"),
    ("أُلغي الطلب", "Order cancelled"),
    ("أُلغي طلبك لانتهاء المهلة دون قبول سائق.", "Your order was cancelled because no driver accepted in time."),
    ("المدفوعات متوقفة مؤقتًا", "Payments are temporarily paused"),
    ("الخدمة متوقفة", "The service is stopped"),
    ("وضع الطوارئ: النظام للقراءة فقط", "Emergency mode: the system is read-only"),
    ("النظام في وضع الطوارئ", "The system is in emergency mode"),
    ("أكواد الخصم غير متاحة حاليًا", "Promo codes are currently unavailable"),
    ("كود الخصم غير صحيح", "The promo code is invalid"),
    ("كود الخصم لم يبدأ بعد", "The promo code is not active yet"),
    ("انتهت صلاحية كود الخصم", "The promo code has expired"),
    ("لا يمكنك استخدام كود الدعوة الخاص بك", "You cannot use your own invite code"),
    ("استخدمتَ كود دعوة من قبل", "You have already used an invite code"),
    ("بلغ هذا الكود الحد الأقصى للاستخدام", "This code has reached its usage limit"),
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
_AR_BY_EN = {en: ar for ar, en in PAIRS if "{" not in ar}
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
    """Sources may be Arabic (older strings) or English (new): `en` returns English, `ar` returns Arabic when a pair exists."""
    if not isinstance(text, str):
        return text
    if lang == "en":
        return to_en(text)
    return _AR_BY_EN.get(text, text)


def localize_detail(detail, lang: str = "en"):
    """Error `detail` is always English (owner rule 2026-11-24): a string, or a dict whose `message`/`note` is translated (codes never change)."""
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
