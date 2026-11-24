# ruff: noqa: E501 - long lines are Arabic prose inside the HTML template
"""Build the two-page Arabic project brief (YEMENDEV_AI_Brief.pdf).

Costs and timeline come from model.py (the same planning assumptions as the full proposal), so the
two documents cannot disagree. Status figures are the verified state of the repository at build time.

    python3 docs/proposal/build_brief.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import model  # noqa: E402

# Verified repository state (update together with docs/IMPLEMENTATION_ROADMAP.md).
STATUS = {
    "tests": 268,           # full suite, 0 skipped, Docker available
    "browser_checks": "34/34",
    "benchmark": "15/15",
    "adrs": 13,
    "stages_done": "0–16",
    "date": "نوفمبر 2026",
}


def k(v: float) -> str:
    """Thousands of USD, rounded."""
    return f"{round(v / 1000):,}"


def rng(lo: float, hi: float) -> str:
    return f'<bdi class="rng" dir="ltr">{k(lo)}–{k(hi)}</bdi>'


FONTS = "".join(
    f"@font-face{{font-family:'Plex';font-weight:{w};src:url(fonts/ibm-plex-sans-arabic-{part}-{w}-normal.woff2)"
    f" format('woff2');unicode-range:{ur};}}"
    for w in (400, 600, 700)
    for part, ur in (("arabic", "U+0600-06FF,U+0750-077F,U+0870-08FF,U+FB50-FDFF,U+FE70-FEFF,U+200C-200F"),
                     ("latin", "U+0000-00FF,U+0131,U+0152-0153,U+02C6,U+02DA,U+02DC,U+2000-206F,U+2190-2199,"
                               "U+2212,U+2215"))
)

CSS = FONTS + """
@page{size:A4;margin:0;}
:root{--teal:#0f6e6e;--teal-d:#0b4f4f;--teal-l:#e7f2f2;--ink:#1d2327;--muted:#5d6670;--gold:#b8860b;
      --red:#b4442e;--line:#dde2e6;}
*{box-sizing:border-box;}
html{direction:rtl;}
body{margin:0;font-family:'Plex',sans-serif;color:var(--ink);font-size:9.4pt;line-height:1.6;}
.page{width:210mm;height:297mm;overflow:hidden;position:relative;break-after:page;padding:0 13mm 14mm;}
.page:last-child{break-after:auto;}
.band{margin:0 -13mm 4mm;padding:8mm 13mm 6mm;color:#fff;
      background:linear-gradient(160deg,#0b4f4f 0%,#0f6e6e 60%,#17807a 100%);}
.band .row{display:flex;justify-content:space-between;align-items:flex-end;gap:6mm;}
.brand{font-size:25pt;font-weight:700;direction:ltr;line-height:1.1;letter-spacing:.5px;}
.full{font-size:8.6pt;opacity:.9;direction:ltr;margin-top:1mm;}
.slogan{font-size:12pt;font-weight:600;}
.band h1{font-size:15pt;margin:4mm 0 0;font-weight:700;}
.band .lead{font-size:9.6pt;opacity:.95;margin:1mm 0 0;}
.band.slim{padding:6mm 13mm 4.5mm;}
.band.slim h1{margin:0;}
h2{font-size:11.6pt;color:var(--teal);margin:3.6mm 0 1.4mm;line-height:1.35;display:flex;align-items:center;gap:2mm;}
h2::before{content:"";width:2.2mm;height:4.4mm;background:var(--teal);border-radius:1px;display:inline-block;}
p{margin:1mm 0 1.6mm;text-align:justify;}
ul{margin:.6mm 0 1.6mm;padding-inline-start:5mm;}
li{margin:.45mm 0;}
.what{border:1px solid var(--line);border-inline-start:5px solid var(--teal);background:#f8fbfb;padding:2.8mm 4mm;
      border-radius:3px;font-size:9.9pt;line-height:1.72;}
.what b{color:var(--teal-d);}
.loop{display:flex;gap:1.6mm;align-items:stretch;margin:1.6mm 0 1mm;}
.step{flex:1;border:1px solid var(--line);border-radius:5px;background:#fff;padding:2.2mm 2.2mm 2mm;position:relative;}
.step .n{position:absolute;top:-2.6mm;inset-inline-start:2mm;background:var(--teal);color:#fff;border-radius:50%;
         width:5.2mm;height:5.2mm;font-size:7.6pt;font-weight:700;display:flex;align-items:center;justify-content:center;}
.step b{display:block;color:var(--teal);font-size:10.6pt;margin-top:.6mm;}
.step span{font-size:8.3pt;color:#34404a;line-height:1.5;display:block;}
.arrow{align-self:center;color:var(--teal);font-size:11pt;font-weight:700;}
.recover{margin:1.4mm 0 0;border:1px dashed var(--teal);border-radius:5px;padding:1.6mm 3mm;background:var(--teal-l);
         font-size:8.7pt;}
.recover b{color:var(--teal-d);}
.chain{display:flex;flex-wrap:wrap;gap:1mm;align-items:center;margin-top:.6mm;}
.chain i{font-style:normal;background:#fff;border:1px solid #b9d6d6;border-radius:9px;padding:0 2mm;font-size:8.3pt;}
.chain i.h{background:var(--gold);color:#fff;border-color:var(--gold);font-weight:600;}
.chain em{font-style:normal;color:var(--teal);font-weight:700;}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:2mm;margin-top:1mm;}
.card{border:1px solid var(--line);border-radius:5px;padding:2mm 2.6mm;background:#fff;}
.card b{display:block;color:var(--teal-d);font-size:9.6pt;margin-bottom:.4mm;}
.card span{font-size:8.4pt;color:#34404a;line-height:1.5;display:block;}
.two{display:flex;gap:4mm;}.two>div{flex:1;}
.kpis{display:flex;gap:2mm;margin:1mm 0 1mm;}
.kpi{flex:1;border:1px solid var(--line);border-radius:6px;padding:1.6mm 1mm;text-align:center;background:#fff;}
.kpi b{display:block;font-size:14pt;color:var(--teal);line-height:1.25;direction:ltr;}
.kpi span{font-size:7.9pt;color:var(--muted);line-height:1.35;display:block;}
.done li::marker{content:"✓  ";color:#17663a;font-weight:700;}
.todo li::marker{content:"○  ";color:var(--gold);font-weight:700;}
.done,.todo{font-size:8.9pt;}
table{width:100%;border-collapse:collapse;font-size:8.9pt;line-height:1.45;margin:1mm 0 .8mm;}
th{background:var(--teal);color:#fff;font-weight:600;padding:1.6mm 2mm;text-align:right;}
td{padding:1.35mm 2mm;border-bottom:1px solid var(--line);vertical-align:middle;}
tr:nth-child(even) td{background:#f7f9fa;}
td.num{white-space:nowrap;font-weight:700;color:var(--teal-d);text-align:left;}
tr.lean td{background:#fffaf0!important;}
.rng{white-space:nowrap;}
.small{font-size:7.9pt;color:var(--muted);line-height:1.5;}
.timeline{position:relative;height:15mm;margin:1mm 2mm 1mm;}
.tl-bar{position:absolute;top:5.6mm;left:0;right:0;height:2.2mm;background:var(--teal-l);border-radius:2mm;}
.tl-fill{position:absolute;top:5.6mm;right:0;height:2.2mm;background:var(--teal);border-radius:2mm;}
.ms{position:absolute;top:0;transform:translateX(50%);text-align:center;font-size:7.9pt;white-space:nowrap;}
.ms .dot{width:4mm;height:4mm;border-radius:50%;background:var(--gold);border:2px solid #fff;margin:4.7mm auto .6mm;
         box-shadow:0 0 0 1px var(--gold);}
.ms b{color:var(--teal-d);}
.next{counter-reset:n;list-style:none;padding:0;margin:.8mm 0;display:grid;grid-template-columns:1fr 1fr;gap:1.4mm 3mm;}
.next li{counter-increment:n;display:flex;gap:2mm;align-items:flex-start;font-size:8.9pt;margin:0;}
.next li::before{content:counter(n);background:var(--teal);color:#fff;border-radius:50%;min-width:4.8mm;height:4.8mm;
                 font-size:7.6pt;font-weight:700;display:flex;align-items:center;justify-content:center;margin-top:.5mm;}
.example{margin:2mm 0 0;border:1px solid #ecd9a6;border-inline-start:5px solid var(--gold);background:#fffaf0;
         padding:2.2mm 3.4mm;border-radius:3px;font-size:9pt;line-height:1.65;}
.example b{color:#7a5c00;}
.risks td{font-size:8.7pt;}
.risks td:first-child{font-weight:600;color:var(--teal-d);width:30%;}
.foot{position:absolute;bottom:6mm;left:13mm;right:13mm;border-top:1px solid var(--line);padding-top:1.6mm;
      display:flex;justify-content:space-between;font-size:7.6pt;color:var(--muted);}
.foot .ltr{direction:ltr;}
"""

ARROW = '<div class="arrow">←</div>'


def page1() -> str:
    steps = [
        ("افهم", "يفحص الشيفرة ويبني «توأمًا رقميًا» يوضّح أجزاء النظام وكيف يعتمد بعضها على بعض."),
        ("تنبّأ", "يقدّر احتمال الأعطال من التاريخ الفعلي للمشروع، ويقول صراحةً متى تكون الأدلة غير كافية."),
        ("احمِ", "يرصد علامات الخلل والتغييرات الخطرة، ويشغّل أي شيفرة غير موثوقة في بيئة معزولة."),
        ("تعافَ", "عند العطل: يشخّص السبب ويقترح إصلاحًا ويجرّبه ويطبّقه بأمان (التفاصيل أدناه)."),
        ("تطوّر", "يحوّل كل إصلاح ناجح إلى مهارة موثّقة يعيد استخدامها، ولا تُفعَّل إلا بموافقة بشرية."),
    ]
    loop = ARROW.join(f'<div class="step"><div class="n">{i}</div><b>{t}</b><span>{d}</span></div>'
                      for i, (t, d) in enumerate(steps, 1))
    chain = '<em>←</em>'.join(f'<i class="h">{x}</i>' if x.startswith("موافقة") else f"<i>{x}</i>" for x in [
        "تشخيص", "اقتراح إصلاح", "تجربة في نسخة معزولة", "موافقة إنسان آخر", "تطبيق", "تحقّق",
        "تراجع تلقائي إن فشل"])
    cards = [
        ("دليل لكل قرار", "كل نتيجة مرتبطة بدليلها، في سجل تدقيق متسلسل يكشف أي تلاعب."),
        ("الإنسان صاحب القرار", "لا تعديل دون موافقة؛ صاحب الطلب لا يوافق على طلبه، والوكلاء الآليون لا يوافقون أبدًا."),
        ("تجربة قبل التطبيق", "كل إصلاح يُختبر أولًا في حاوية معزولة بلا شبكة، مع تراجع مضمون إلى الحالة الأصلية."),
        ("يعمل دون إنترنت", "التحليل والإصلاح والواجهة تعمل محليًا، والمزامنة بين الأجهزة عبر حزم موقّعة رقميًا."),
        ("يتعلّم ويتذكّر", "ذاكرة حيّة لكل معلومة فيها مصدرها ودرجة الثقة بها، وتكشف التناقضات والمعلومات القديمة."),
        ("عربي أولًا", "واجهة عربية كاملة من اليمين إلى اليسار، مع الإنجليزية، وتوثيق بالعربية."),
    ]
    grid = "".join(f'<div class="card"><b>{t}</b><span>{d}</span></div>' for t, d in cards)
    return f"""
<section class="page">
  <div class="band">
    <div class="row">
      <div><div class="brand">YEMENDEV AI</div>
           <div class="full">Global Autonomous Digital Resilience &amp; Evolution Platform</div></div>
      <div class="slogan">افهم · تنبّأ · احمِ · تعافَ · تطوّر</div>
    </div>
    <h1>فكرة المشروع في صفحتين</h1>
    <p class="lead">منصة المرونة الرقمية: تحمي الأنظمة البرمجية من الأعطال وتعيدها للعمل بأمان، حتى دون إنترنت.</p>
  </div>

  <div class="what">
    <b>ما هو المشروع؟</b> YEMENDEV AI منصة ذكية تعمل مثل <b>«طبيب دائم» للأنظمة البرمجية</b>: تفهم بنية
    المشروع، وتتنبأ بأعطاله قبل وقوعها، وعند حدوث عطل تشخّصه وتقترح إصلاحًا <b>تجرّبه أولًا في بيئة معزولة،
    ولا تطبّقه إلا بموافقة إنسان</b>، ثم تتحقق من نجاحه وتتراجع عنه تلقائيًا إن فشل، وتتعلّم من كل إصلاح ناجح.
    كل خطوة تُسجَّل بدليل يمكن مراجعته، والمنصة تعمل محليًا دون الحاجة إلى اتصال دائم بالإنترنت.
  </div>

  <h2>المشكلة</h2>
  <ul>
    <li>الأنظمة البرمجية تتعطّل باستمرار: تحديث مكتبة، أو تغيير في واجهة برمجية، أو خطأ بشري صغير قد يوقف خدمة كاملة.</li>
    <li>الإصلاح اليدوي بطيء ومكلف، ويعتمد على خبراء قد لا يتوفرون وقت الحاجة.</li>
    <li>أدوات الذكاء الاصطناعي الحالية تقترح تعديلات دون دليل على صحتها، ودون ضمان للتراجع، وتحتاج إنترنت دائمًا.</li>
    <li>في بيئات مثل اليمن، حيث انقطاع الكهرباء وضعف الاتصال، تشتد هذه المشكلات ولا توجد أدوات مصممة لها.</li>
  </ul>

  <h2>كيف يعمل؟ دورة من خمس مراحل</h2>
  <div class="loop">{loop}</div>
  <div class="recover"><b>مرحلة «تعافَ» بالتفصيل:</b>
    <div class="chain">{chain}</div>
  </div>

  <div class="example"><b>مثال حقيقي من اختبارات المشروع:</b> مكتبة داخلية غيّرت اسم دالة فتعطّلت اختبارات
    المشروع. حدّدت المنصة السبب، واقترحت تعديل الاستدعاء، وجرّبته في نسخة معزولة، وانتظرت موافقة مهندس ثانٍ، ثم
    طبّقته وتحقّقت أن كل الاختبارات تنجح. بعد ذلك حوّلت الإصلاح إلى مهارة، فأصلحت بها مشروعًا آخر فيه العطل نفسه
    (بعد التحقق والموافقة أيضًا).</div>

  <h2>ما الذي يميّزه؟</h2>
  <div class="grid">{grid}</div>

  <h2>لمن هذا المشروع؟</h2>
  <p>شركات البرمجيات الصغيرة والمتوسطة، والمؤسسات والجهات الحكومية في بيئات الاتصال الضعيف، وفرق التطوير التي
  تدير أنظمة حرجة، والجامعات ومراكز البحث المهتمة بموثوقية البرمجيات والذكاء الاصطناعي الآمن.</p>

  <div class="foot"><span>YEMENDEV AI · فكرة المشروع</span><span class="ltr">1 / 2</span></div>
</section>"""


def page2() -> str:
    s = STATUS
    kpis = [(str(s["tests"]), "اختبارًا آليًا ناجحًا"), (s["stages_done"], "مراحل مكتملة ومختبرة"),
            (s["benchmark"], "سيناريو أعطال في مقياس المرونة"), (s["browser_checks"], "فحصًا للواجهة في متصفح حقيقي"),
            (str(s["adrs"]), "قرارًا معماريًا موثّقًا")]
    kpi = "".join(f'<div class="kpi"><b>{v}</b><span>{t}</span></div>' for v, t in kpis)
    done = [
        "فحص مشاريع بايثون وبناء رسم الترابط وتحديد الوحدات الحرجة.",
        "تشغيل الاختبارات في بيئة معزولة (حاويات Docker أو عزل العمليات).",
        "دورة الإصلاح كاملة: تشخيص، اقتراح، محاكاة، موافقة، تطبيق، تحقّق، تراجع.",
        "محرك المخاطر والتنبؤ، والتحليل السببي، ونظام المناعة، ومحرك «ماذا لو».",
        "ذاكرة حيّة ومهارات متعلَّمة، و8 وكلاء بصلاحيات محددة لا يتجاوزونها.",
        "مزامنة دون اتصال بحزم موقّعة، و«جواز استمرارية» رقمي للنظام.",
        "واجهة برمجية وسطر أوامر وواجهة ويب بالعربية والإنجليزية.",
    ]
    todo = [
        "تقييم الإصلاحات مع نموذج ذكاء اصطناعي حقيقي (يحتاج نموذجًا وعتادًا).",
        "مقارنة علمية مع أنظمة أخرى.",
        "طابور المهام والجدولة: الجزء الأول مكتمل، والباقي قيد التنفيذ.",
        "دعم JavaScript/TypeScript (حاليًا بايثون فقط).",
        "تطبيقا سطح المكتب والجوال.",
        "تدقيق أمني خارجي قبل الإصدار 1.0.",
    ]
    lean = model.lean_totals()
    rows = [f'<tr class="lean"><td><b>المسار المختصر</b></td><td>6 أشهر</td><td>3 أفراد</td>'
            f'<td>إصدار تجريبي خاص للتجربة مع عملاء أوائل</td><td class="num">{rng(lean["low"], lean["high"])}</td></tr>']
    labels = {"A": "فريق إقليمي عن بُعد", "B": "فريق مختلط: إقليمي وخبراء دوليون", "C": "أسعار السوق الدولية"}
    for key, letter in (("A", "أ"), ("B", "ب"), ("C", "ج")):
        t = model.totals(key)
        rows.append(f'<tr><td><b>السيناريو {letter}</b>: {labels[key]}</td><td>12 شهرًا</td><td>11 دورًا</td>'
                    f'<td>الإصدار 1.0 كاملًا</td><td class="num">{rng(t["low"], t["high"])}</td></tr>')
    pm = round(sum(model.person_months().values()))
    milestones = "".join(
        f'<div class="ms" style="right:{(m / 12) * 100:.2f}%"><div class="dot"></div><b>الشهر {m}</b><br>{t.split(": ")[1]}</div>'
        for m, t in model.MILESTONES)
    nxt = [
        "اختيار مسار التمويل: المختصر أو أحد السيناريوهات.",
        "اختيار ترخيص المشروع: مفتوح المصدر أو تجاري.",
        "توفير نموذج ذكاء اصطناعي (محلي أو عبر خدمة) لتقييم جودة الإصلاحات.",
        "جهة أو جهتان تجريبيتان تستخدمان الإصدار التجريبي على أنظمة حقيقية.",
    ]
    return f"""
<section class="page">
  <div class="band slim"><h1>أين وصلنا؟ وماذا يلزم للإصدار الأول؟</h1>
    <p class="lead">الوضع الفعلي للمشروع كما تحقّقنا منه في {s["date"]}، والتكلفة والمدة المتوقعتان.</p></div>

  <div class="kpis">{kpi}</div>

  <div class="two">
    <div><h2>ما تم بناؤه فعليًا ويعمل</h2><ul class="done">{"".join(f"<li>{x}</li>" for x in done)}</ul></div>
    <div><h2>ما لم يُنجز بعد</h2><ul class="todo">{"".join(f"<li>{x}</li>" for x in todo)}</ul></div>
  </div>

  <h2>التكلفة والمدة (بآلاف الدولارات)</h2>
  <table>
    <tr><th>المسار</th><th>المدة</th><th>الفريق</th><th>النتيجة</th><th>التكلفة التقديرية</th></tr>
    {"".join(rows)}
  </table>
  <p class="small">الأرقام تقديرات تخطيطية وليست عروض أسعار، وتشمل احتياطيًا {round(model.CONTINGENCY * 100)}٪ للطوارئ.
  الفريق الكامل للإصدار 1.0 يضم 11 دورًا ({pm} شهر عمل): قائد تقني، ومهندسو خلفية، ومهندس ذكاء اصطناعي، ومهندس أمن،
  ومهندس واجهات، ومهندس سطح مكتب وجوال، وجودة وتشغيل، ومصمم، وباحث، وكاتب تقني. وتشمل التكلفة الخوادم وتقييم
  النماذج وأجهزة الفريق والطاقة الاحتياطية والتدقيق الأمني الخارجي.</p>

  <h2>الجدول الزمني للإصدار 1.0 (12 شهرًا)</h2>
  <div class="timeline"><div class="tl-bar"></div>{milestones}</div>

  <h2>أهم المخاطر وكيف نتعامل معها</h2>
  <table class="risks">
    <tr><th>الخطر</th><th>طريقة التعامل</th></tr>
    <tr><td>جودة إصلاحات الذكاء الاصطناعي غير مُثبتة بعد</td><td>المنصة تعمل وتُصلح دون نموذج (بالمهارات والخطط
      البشرية)، والنموذج يقترح فقط؛ لا شيء يُطبَّق دون تجربة معزولة وموافقة بشرية وتحقّق، والتقييم مخطط في الأشهر 2–7.</td></tr>
    <tr><td>ثغرة أمنية تسمح لشيفرة المشروع بالإضرار بالجهاز</td><td>تشغيل معزول بلا شبكة وبلا صلاحيات، واختبارات هجوم
      آلية؛ ومراجعتان أمنيتان داخليتان اكتشفتا ثغرتين حرجتين وأُصلحتا مع اختبارات تمنع عودتهما، ثم تدقيق خارجي قبل الإصدار 1.0.</td></tr>
    <tr><td>التمويل أو صعوبة توفير الفريق</td><td>المسار المختصر (3 أفراد، 6 أشهر) يوصل إلى إصدار تجريبي يثبت القيمة
      قبل الالتزام بالفريق الكامل.</td></tr>
  </table>

  <h2>المطلوب لاتخاذ الخطوة التالية</h2>
  <ol class="next">{"".join(f"<li>{x}</li>" for x in nxt)}</ol>

  <div class="foot"><span>التفاصيل الكاملة في «وثيقة المشروع الشاملة» (22 صفحة) في المستودع: docs/proposal</span>
    <span class="ltr">2 / 2</span></div>
</section>"""


def build() -> Path:
    html = (f'<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><title>YEMENDEV AI — فكرة المشروع'
            f'</title><style>{CSS}</style></head><body>{page1()}{page2()}</body></html>')
    out_html = HERE / "YEMENDEV_AI_Brief.html"
    out_html.write_text(html, encoding="utf-8")
    out_pdf = HERE / "YEMENDEV_AI_Brief.pdf"
    subprocess.run(["node", str(HERE / "print_brief.mjs"), str(out_html), str(out_pdf)], check=True)
    return out_pdf


if __name__ == "__main__":
    print(build())
