"""PDF / XLSX exports of the growth report (for partners and investors). Arabic is shaped with arabic-reshaper + bidi."""
import io
from pathlib import Path

FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"

T = {  # key: (ar, en)
    "title": ("تقرير النمو والإيرادات — وصلي", "Wasaly — Growth & Revenue Report"), "period": ("الفترة", "Period"),
    "kpis": ("المؤشرات الرئيسية", "Key metrics"), "metric": ("المؤشر", "Metric"), "value": ("القيمة", "Value"), "change": ("التغير %", "Change %"),
    "investor": ("مؤشرات للمستثمرين", "Investor metrics"), "daily": ("اليومي", "Daily"), "date": ("التاريخ", "Date"),
    "bookings": ("الحجوزات", "Bookings"), "completed": ("المكتملة", "Completed"), "commission": ("العمولة", "Commission"),
    "partners": ("أداء الشركاء", "Partner performance"), "partner": ("الشريك", "Partner"), "share": ("حصته", "Share"), "paid": ("المدفوع", "Paid"),
    "trips": ("الرحلات", "Trips"), "percent": ("النسبة %", "Percent"), "cancels": ("الإلغاءات حسب السبب", "Cancellations by reason"),
    "reason": ("السبب", "Reason"), "count": ("العدد", "Count"), "country": ("الدولة", "Country"), "gmv": ("قيمة الرحلات", "Gross trip value"),
    "by_country": ("حسب الدولة", "By country"), "note": ("الأرقام بالعملة المحلية وبتوقيت البحرين. التغير مقارنة بالفترة السابقة المماثلة.",
                                                         "Amounts in local currency, Bahrain time. Change is versus the previous equal period."),
    "k_bookings": ("إجمالي الحجوزات", "Total bookings"), "k_completed": ("الرحلات المكتملة", "Completed trips"),
    "k_commission": ("عمولات وصلي", "Commission revenue"), "k_gmv": ("قيمة الرحلات", "Gross trip value"),
    "k_active_drivers": ("السائقون النشطون", "Active drivers"), "k_active_customers": ("العملاء النشطون", "Active customers"),
    "k_new_customers": ("عملاء جدد", "New customers"), "k_cac": ("تكلفة اكتساب العميل", "CAC"),
    "k_repeat_rate": ("نسبة تكرار الاستخدام %", "Repeat rate %"), "k_completion_rate": ("نسبة الإكمال %", "Completion rate %"),
    "k_cancel_rate": ("نسبة الإلغاء %", "Cancellation rate %"), "k_refunds": ("المسترجعات", "Refunds"),
    "i_avg_fare": ("متوسط قيمة الرحلة", "Average fare"), "i_avg_commission_per_trip": ("متوسط العمولة للرحلة", "Average commission per trip"),
    "i_take_rate_pct": ("نسبة العمولة من القيمة %", "Take rate %"), "i_driver_payouts": ("مستحقات السائقين", "Driver earnings"),
    "i_promo_discounts": ("خصومات العروض", "Promo discounts"), "i_referral_cost": ("تكلفة الدعوات", "Referral cost"),
    "i_net_profit": ("صافي ربح الشركة", "Company net profit"), "i_avg_trip_minutes": ("متوسط مدة الرحلة (دقيقة)", "Average trip (min)"),
    "i_trips_per_active_driver": ("رحلات لكل سائق نشط", "Trips per active driver"),
    "i_trips_per_active_customer": ("رحلات لكل عميل نشط", "Trips per active customer"),
    "i_customers_total": ("إجمالي العملاء", "Total customers"), "i_drivers_total": ("إجمالي السائقين", "Total drivers"),
    "i_wallet_liability": ("أرصدة المحافظ (التزام)", "Wallet balances (liability)"),
    "i_driver_debt_owed_to_company": ("ديون السائقين للشركة", "Driver debt owed to the company"),
    "peak": ("ساعات الذروة", "Peak hours"), "hour": ("الساعة", "Hour"), "mix": ("طرق الدفع", "Payment methods"),
    "method": ("الطريقة", "Method"), "amount": ("المبلغ", "Amount"), "payments": ("الدفعات", "Payments"),
}
KPI_ORDER = ["bookings", "completed", "commission", "gmv", "active_drivers", "active_customers", "new_customers", "cac",
             "repeat_rate", "completion_rate", "cancel_rate", "refunds"]
INV_SCALARS = ["avg_fare", "avg_commission_per_trip", "take_rate_pct", "driver_payouts", "promo_discounts", "referral_cost", "net_profit",
               "avg_trip_minutes", "trips_per_active_driver", "trips_per_active_customer", "customers_total", "drivers_total",
               "wallet_liability", "driver_debt_owed_to_company"]


def _t(key: str, lang: str) -> str:
    return T[key][1 if lang == "en" else 0]


def _dash(v) -> str:
    return "—" if v is None else str(v)


def to_xlsx(g: dict, lang: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    head = PatternFill("solid", fgColor="1F3A5F")
    hfont = Font(bold=True, color="FFFFFF")
    rtl = lang != "en"

    def sheet(name, headers, rows, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = name[:30]
        ws.sheet_view.rightToLeft = rtl
        ws.append(headers)
        for c in ws[1]:
            c.fill, c.font, c.alignment = head, hfont, Alignment(horizontal="center")
        for r in rows:
            ws.append(r)
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(48, max(12, max(len(str(c.value or "")) for c in col) + 2))
        return ws

    k = g["kpis"]
    sheet(_t("kpis", lang), [_t("metric", lang), _t("value", lang), _t("change", lang)],
          [[_t("k_" + key, lang), k[key]["value"], k[key]["change_pct"]] for key in KPI_ORDER], first=True)
    inv = g["investor"]
    sheet(_t("investor", lang), [_t("metric", lang), _t("value", lang)], [[_t("i_" + key, lang), inv[key]] for key in INV_SCALARS])
    sheet(_t("daily", lang), [_t("date", lang), _t("bookings", lang), _t("completed", lang), _t("commission", lang)],
          [[d["date"], d["bookings"], d["completed"], float(d["commission"])] for d in g["daily"]])
    sheet(_t("partners", lang), [_t("partner", lang), _t("percent", lang), _t("trips", lang), _t("share", lang), _t("paid", lang)],
          [[p["name"], float(p["percent"]), p["trips"], float(p["share"]), float(p["paid"])] for p in g["partners"]])
    sheet(_t("cancels", lang), [_t("reason", lang), _t("count", lang)], [[c["label"], c["count"]] for c in g["cancellations"]])
    sheet(_t("by_country", lang), [_t("country", lang), _t("completed", lang), _t("gmv", lang), _t("commission", lang)],
          [[c["country"], c["completed"], float(c["gmv"]), float(c["commission"])] for c in g["by_country"]])
    sheet(_t("peak", lang), [_t("hour", lang), _t("trips", lang)], [[f'{h["hour"]:02d}:00', h["trips"]] for h in inv["peak_hours"]])
    sheet(_t("mix", lang), [_t("method", lang), _t("payments", lang), _t("amount", lang)],
          [[m["method"], m["payments"], float(m["amount"])] for m in inv["payment_mix"]])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_pdf(g: dict, lang: str) -> bytes:
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    for name, f in (("DV", "DejaVuSans.ttf"), ("DV-B", "DejaVuSans-Bold.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(FONTS / f)))
    rtl = lang != "en"
    if rtl:
        import arabic_reshaper
        from bidi.algorithm import get_display

        def s(x):
            return get_display(arabic_reshaper.reshape(str(x)))
    else:
        def s(x):
            return str(x)
    align = 2 if rtl else 0
    h1 = ParagraphStyle("h1", fontName="DV-B", fontSize=16, alignment=align, spaceAfter=4)
    h2 = ParagraphStyle("h2", fontName="DV-B", fontSize=12, alignment=align, spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#1F3A5F"))
    small = ParagraphStyle("sm", fontName="DV", fontSize=8, alignment=align, textColor=colors.grey)

    def table(headers, rows, widths=None):
        data = [[s(x) for x in headers]] + [[s(_dash(x)) for x in r] for r in rows]
        if rtl:
            data = [list(reversed(r)) for r in data]
            widths = list(reversed(widths)) if widths else None
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), "DV"), ("FONTNAME", (0, 0), (-1, 0), "DV-B"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                               ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3A5F")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F5F9")]),
                               ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E1")), ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
        return t

    el = [Paragraph(s(_t("title", lang)), h1),
          Paragraph(s(f'{_t("period", lang)}: {g["from"]} → {g["to"]} ({g["days"]}) · {g["currency"]}'), small), Spacer(1, 6)]
    k = g["kpis"]
    el += [Paragraph(s(_t("kpis", lang)), h2),
           table([_t("metric", lang), _t("value", lang), _t("change", lang)],
                 [[_t("k_" + key, lang), k[key]["value"], k[key]["change_pct"]] for key in KPI_ORDER], [80 * mm, 45 * mm, 35 * mm])]
    series = [(i, float(d["commission"])) for i, d in enumerate(g["daily"])]
    if len(series) > 1:
        dr = Drawing(160 * mm, 55 * mm)
        lp = LinePlot()
        lp.x, lp.y, lp.width, lp.height = 12 * mm, 8 * mm, 140 * mm, 42 * mm
        lp.data = [series]
        lp.lines[0].strokeColor = colors.HexColor("#1F3A5F")
        lp.lines[0].strokeWidth = 1.4
        lp.xValueAxis.visible = False
        lp.yValueAxis.labels.fontName, lp.yValueAxis.labels.fontSize = "DV", 7
        dr.add(lp)
        el += [Paragraph(s(_t("daily", lang) + " — " + _t("commission", lang)), h2), dr]
    inv = g["investor"]
    el += [Paragraph(s(_t("investor", lang)), h2),
           table([_t("metric", lang), _t("value", lang)], [[_t("i_" + key, lang), inv[key]] for key in INV_SCALARS], [100 * mm, 60 * mm])]
    if g["partners"]:
        el += [Paragraph(s(_t("partners", lang)), h2),
               table([_t("partner", lang), _t("percent", lang), _t("share", lang), _t("paid", lang)],
                     [[p["name"], p["percent"], p["share"], p["paid"]] for p in g["partners"]], [55 * mm, 30 * mm, 38 * mm, 38 * mm])]
    if g["cancellations"]:
        el += [Paragraph(s(_t("cancels", lang)), h2), table([_t("reason", lang), _t("count", lang)], [[c["label"], c["count"]] for c in g["cancellations"]], [100 * mm, 40 * mm])]
    if inv["payment_mix"]:
        el += [Paragraph(s(_t("mix", lang)), h2), table([_t("method", lang), _t("payments", lang), _t("amount", lang)],
                                                         [[m["method"], m["payments"], m["amount"]] for m in inv["payment_mix"]], [60 * mm, 40 * mm, 50 * mm])]
    if inv["peak_hours"]:
        el += [Paragraph(s(_t("peak", lang)), h2), table([_t("hour", lang), _t("trips", lang)], [[f'{h["hour"]:02d}:00', h["trips"]] for h in inv["peak_hours"]], [50 * mm, 40 * mm])]
    el += [Spacer(1, 8), Paragraph(s(_t("note", lang)), small)]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                      title=_t("title", lang)).build(el)
    return buf.getvalue()
