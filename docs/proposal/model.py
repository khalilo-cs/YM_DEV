"""Planning data for the project proposal: team, timeline, costs.

ALL monetary values are PLANNING ASSUMPTIONS (USD), not market data. Replace the rates with real
local quotes before using them for a budget. Totals are computed here so the PDF arithmetic is exact.
"""

from __future__ import annotations

# ---- timeline (v1.0 plan, 12 months) -------------------------------------------------------------
WORKSTREAMS = [  # (arabic name, english, start_month, end_month, kind)
    ("تثبيت النواة وإصدار تجريبي خاص", "Core hardening & private beta", 1, 3, "core"),
    ("الأمن المستمر والتدقيق الخارجي", "Security hardening & external audit", 1, 12, "sec"),
    ("تكامل النماذج المحلية والبعيدة وتقييمها", "Model integration & evaluation", 2, 7, "ai"),
    ("البحث والمقاييس المقارنة", "Research & comparative benchmarks", 3, 12, "res"),
    ("واجهة الويب ولوحة التحكم + العربية/الإنجليزية", "Web UI, dashboard, i18n", 4, 9, "ui"),
    ("الاستمرارية والمزامنة دون اتصال + PostgreSQL", "Offline sync & multi-user", 5, 9, "core"),
    ("محرك المخاطر والتوأم الرقمي والمحاكاة", "Risk engine, digital twin, simulation", 6, 11, "ai"),
    ("تطبيق سطح المكتب والتطبيق المرافق للجوال", "Desktop & Android companion", 8, 12, "ui"),
    ("الجاهزية للإطلاق: تغليف، توثيق، دعم", "Release readiness", 11, 12, "core"),
]
MILESTONES = [(3, "M1: إصدار تجريبي خاص"), (7, "M2: إصدار تجريبي عام"), (12, "M3: الإصدار 1.0")]

# ---- team (person-months over the 12-month plan) -------------------------------------------------
# (arabic role, english role, rate class, FTE, months)
TEAM = [
    ("قائد تقني / مهندس معماري", "Tech lead / architect", "senior", 1.0, 12),
    ("مهندس خلفية أول (Python)", "Senior backend engineer", "senior", 1.0, 12),
    ("مهندس خلفية", "Backend engineer", "mid", 1.0, 10),
    ("مهندس ذكاء اصطناعي / نماذج لغوية", "ML / LLM engineer", "specialist", 1.0, 9),
    ("مهندس أمن (دوام جزئي)", "Security engineer (part-time)", "specialist", 0.5, 12),
    ("مهندس واجهات (React/TypeScript)", "Frontend engineer", "mid", 1.0, 8),
    ("مهندس سطح مكتب/جوال (Tauri/Kotlin)", "Desktop / mobile engineer", "mid", 1.0, 5),
    ("مهندس جودة وتشغيل (QA/DevOps)", "QA / DevOps engineer", "mid", 1.0, 10),
    ("مصمم منتج وتجربة مستخدم (جزئي)", "Product / UX designer (part-time)", "mid", 0.5, 8),
    ("باحث (جزئي / شراكة جامعية)", "Researcher (part-time / university)", "specialist", 0.5, 12),
    ("كاتب تقني ومترجم عربي (جزئي)", "Technical writer / Arabic l10n", "mid", 0.5, 6),
]

# Fully-loaded monthly cost per person (USD) - ASSUMPTIONS for three scenarios.
SCENARIOS = {
    "A": {"ar": "أ: فريق إقليمي عن بُعد", "en": "Regional remote team",
          "rates": {"senior": 2000, "mid": 1200, "specialist": 2500}},
    "B": {"ar": "ب: فريق مختلط (إقليمي + خبراء دوليون)", "en": "Mixed regional + international experts",
          "rates": {"senior": 3500, "mid": 1800, "specialist": 5000}},
    "C": {"ar": "ج: أسعار السوق الدولية", "en": "International market rates",
          "rates": {"senior": 9000, "mid": 6500, "specialist": 11000}},
}

# Non-personnel items over 12 months: (arabic, low, high, note)
NON_PERSONNEL = [
    ("خوادم سحابية للتطوير والاختبار والتجهيز", 150 * 12, 400 * 12, "150–400 $ شهريًا"),
    ("وحدات معالجة رسومية لتقييم النماذج (حسب الطلب، 8 أشهر)", 300 * 8, 900 * 8, "300–900 $ شهريًا"),
    ("استهلاك واجهات النماذج اللغوية (تطوير وتقييم)", 200 * 12, 1500 * 12,
     "200–1,500 $ شهريًا حسب الاستخدام؛ يجب التحقق من الأسعار الحالية"),
    ("أدوات التطوير والاستضافة والتكامل المستمر", 100 * 12, 300 * 12, "100–300 $ شهريًا"),
    ("أجهزة للفريق (9 أجهزة، مرة واحدة)", 9 * 1000, 9 * 1800, "1,000–1,800 $ للجهاز"),
    ("طاقة احتياطية للفريق (UPS/طاقة شمسية، 9 أفراد)", 9 * 300, 9 * 800, "مهم في بيئات انقطاع الكهرباء"),
    ("بدل اتصال بالإنترنت (9 أفراد × 12 شهرًا)", 9 * 30 * 12, 9 * 80 * 12, "30–80 $ شهريًا للفرد"),
    ("تدقيق أمني واختبار اختراق خارجي قبل الإصدار 1.0", 5000, 20000, "مرة واحدة"),
    ("قانوني: تسجيل شركة، علامة تجارية، تراخيص، سياسة خصوصية", 2000, 8000, "يختلف حسب الدولة"),
    ("حسابات متاجر التطبيقات وشهادات توقيع الكود", 500, 1000, "Google Play وApple وتوقيع سطح المكتب"),
    ("البحث العلمي: رسوم نشر ومؤتمرات", 2000, 8000, ""),
    ("أبحاث السوق وبرامج تجريبية مع عملاء أوائل", 3000, 10000, ""),
]
CONTINGENCY = 0.15

# Lean path: private beta in 6 months with 3 people (scenario A rates)
LEAN_TEAM = [("قائد تقني", "senior", 1.0, 6), ("مهندس خلفية", "mid", 1.0, 6), ("أمن وجودة (جزئي)", "specialist", 0.5, 6)]
LEAN_NON_PERSONNEL = (3000, 9000)


def person_months() -> dict[str, float]:
    pm: dict[str, float] = {"senior": 0.0, "mid": 0.0, "specialist": 0.0}
    for _, _, cls, fte, months in TEAM:
        pm[cls] += fte * months
    return pm


def personnel_cost(scn: str) -> float:
    r = SCENARIOS[scn]["rates"]
    return sum(fte * months * r[cls] for _, _, cls, fte, months in TEAM)


def non_personnel() -> tuple[float, float]:
    return sum(x[1] for x in NON_PERSONNEL), sum(x[2] for x in NON_PERSONNEL)


def totals(scn: str) -> dict[str, float]:
    p = personnel_cost(scn)
    lo, hi = non_personnel()
    return {"personnel": p, "np_low": lo, "np_high": hi,
            "low": (p + lo) * (1 + CONTINGENCY), "high": (p + hi) * (1 + CONTINGENCY)}


def lean_totals() -> dict[str, float]:
    r = SCENARIOS["A"]["rates"]
    p = sum(fte * m * r[cls] for _, cls, fte, m in LEAN_TEAM)
    lo, hi = LEAN_NON_PERSONNEL
    return {"personnel": p, "low": (p + lo) * (1 + CONTINGENCY), "high": (p + hi) * (1 + CONTINGENCY),
            "pm": sum(fte * m for _, _, fte, m in LEAN_TEAM)}


if __name__ == "__main__":
    print("person-months:", person_months(), "total", sum(person_months().values()))
    for k in SCENARIOS:
        t = totals(k)
        print(k, {x: round(v) for x, v in t.items()})
    print("lean", {x: round(v) for x, v in lean_totals().items()})
