# خصم | Khasm

خصم موقع مجاني بيتابع أسعار منتجات نون مصر وأمازون مصر، وبيحاول يوضح النزول الحقيقي بمقارنة السعر بتاريخ **نفس المنتج**، مش بالسعر المشطوب. الموقع المتوقع بعد التفعيل: **https://fy881.github.io/khasm/**.

## شغال إزاي؟

- GitHub Actions بيحاول يقرأ صفحات بحث من المتجرين كل ٣ ساعات، وبيسجل الأسعار في `data/prices/` كسجل تراكمي لا يُحذف. ملف `data/products.json` نسخة صغيرة للعرض؛ الموقع ثابت على GitHub Pages، من غير خادم أو API مدفوع أو أسرار.
- محتاجين ٣ قراءات سابقة على يومين مختلفين على الأقل. قبل كده بنقول «جديد – بنتابع السعر» بدل ما نخمّن. بعد كده بنقارن السعر بوسيط القراءات خلال آخر ٣٠ يوم؛ نزول ٥٪ أو أكثر بنعرضه «نزول حقيقي».
- «خصم وهمي» تنبيه تقريبي: السعر المشطوب أعلى من الوسيط بـ١٥٪ على الأقل، أو في ارتفاع ملحوظ قبل رجوع السعر لمستواه. النزول الحقيقي المثبت بتاريخ السعر له الأولوية. «أرخص سعر من X يوم» مبني فقط على الفترة اللي رصدناها، مش وعد بأقل سعر في السوق.
- المتاجر ممكن تمنع القراءة أو تغير شكل صفحاتها. لو صفحة ما اتقرتش مش بنخترع أسعار؛ القراءات القديمة بتفضل محفوظة ومعلّمة بتاريخها. راجع السعر والتوافر في المتجر قبل الشراء، لأنهما ممكن يتغيروا بين تحديث وآخر.

## أول تجربة

في تجربة ٧ نوفمبر ٢٠٢٦، اتقرأت صفحات أمازون العشر وسُجل ٤٦٨ منتجًا. صفحات نون العشر لم ترجع منتجات: صفحة ردّت بحظر 403 والباقي انتهت مهلته. لذلك اللقطة الأولى فيها أمازون فقط؛ دعم نون محتاج تجربة ناجحة من بيئة التحديث، ومش مضمون. كل المنتجات لسه جديدة لحد ما يتجمع تاريخ كفاية.

## التفعيل مرة واحدة بعد دمج طلب السحب

من إعدادات المستودع على GitHub:
1. **Settings → Pages → Build and deployment → Source: GitHub Actions**.
2. **Settings → Actions → General → Workflow permissions → Read and write permissions → Save** عشان التحديث التلقائي يقدر يحفظ البيانات.

بعد الدمج والتفعيل، شغّل **Actions → Update prices → Run workflow** مرة للتجربة، أو انتظر الجدول التلقائي. النشر بيتم بعد تحديث الأسعار وكمان عند أي push إلى `main`. توقيت cron مش مضمون بالدقيقة، ونجاح التحديث يعتمد على إتاحة صفحات المتاجر. لو حماية الفرع تمنع كتابة Actions، لازم تسمح بالتحديث أو تعدل طريقة استقبال البيانات.

## تشغيل واختبار محلي (اختياري)

```sh
python3 -m pip install -r scraper/requirements.txt pytest
python3 -m scraper.scrape
python3 -m pytest -q
python3 -m http.server 8000
```

افتح `http://localhost:8000/`. التشغيل المحلي يضيف قراءات حقيقية إلى السجل الموجود؛ لا تمسح ملفات التاريخ لو عايز تحتفظ بالمقارنات. تفاصيل الصفحات المحجوبة تظهر في سجل تشغيل Actions. مفيش مفاتيح أو اشتراكات مطلوبة. GitHub Pages وActions متاحان مجانًا للمستودعات العامة ضمن سياسات وحدود GitHub؛ الاعتماد على صفحات المتاجر الخارجية ليس مضمونًا.

---

# English

Khasm is a free, Arabic-first price tracker for Noon Egypt and Amazon Egypt. Its intended live URL after setup is **https://fy881.github.io/khasm/**. It compares each product's current price with the median of its own observed prices over the last 30 days, not just the store's struck-through price. At least three prior observations across two dates are required; a drop of at least 5% is marked as real. Suspicious list prices or pre-discount hikes are flagged, while genuinely lower historical prices take precedence. Lowest-price labels describe only the observed period.

A GitHub Actions schedule runs every three hours and appends observations to `data/prices/`; `data/products.json` is the static site's compact view. Blocked pages and outdated observations are not invented or silently presented as current. Prices and availability should be checked at the store. The project uses public GitHub Pages and Actions without paid services or secrets, subject to GitHub's policies and the stores' availability.

**One-time owner setup after merging:** in **Settings → Pages**, select **GitHub Actions** as the source; in **Settings → Actions → General → Workflow permissions**, select **Read and write permissions** and save. Then optionally run **Actions → Update prices → Run workflow**. Normal pushes to `main` deploy Pages; the scrape workflow also deploys explicitly because commits made with `GITHUB_TOKEN` do not trigger another push workflow. Scheduled runs can be delayed, and branch protection must permit the automated commit.

For local development, install `scraper/requirements.txt` and `pytest`, run `python3 -m scraper.scrape`, `python3 -m pytest -q`, and `python3 -m http.server 8000`. The scrape appends to existing history; keep the history files to retain comparisons.
