# راهنمای جامع ساختار دیتاست، مدل اینسترومنتیشن و تطبیق با کد منبع (`efl-dataset-v2`)

این سند ساختار استاندارد و بهینه‌شده خروجی ابزارهای **Siemens Instrumenter** و **Defects4J Instrumenter** و نحوه مصرف آن‌ها در **EFL** را تعریف می‌کند تا هر زمان با دیتاست‌های تولیدشده کار کردید، به‌سادگی بدانید:
1. برنامه چگونه اینسترومنت شده است؛
2. هر فایل و هر ستون در دیتاست خروجی چه معنایی دارد؛
3. هر رویداد مسیر اجرا (`trace`) و هر `test_id` دقیقاً به کدام فایل، خط، متد و تست‌کیس در کد منبع اولیه و نسخه خطادار Siemens یا Defects4J مربوط می‌شود.

---

## ۱. مقایسه فرمت قبلی با ساختار جدید (`efl-dataset-v2`)

در نسخه قبلی، هر مسیر اجرا هم‌زمان در چند فایل حجیم و تکراری (`cmp.v0.*.csv`, `v*_dataset.txt`, `v*_tagged.txt`, `v*_detailed.json`, کپی در `work/datasets/` یا `output/datasets/`، و `trace.bin`های خام) ذخیره می‌شد و کدهای C زیمنس نیز در دو مسیر تکراری کامپایل می‌شدند. در ساختار جدید **فایل‌های تکراری و قدیمی دیگر تولید نمی‌شوند**:

| بخش | ساختار قبلی (حذف‌شده) | ساختار جدید (`efl-dataset-v2`) | مزیت |
|---|---|---|---|
| **مسیرهای اجرا** | `cmp.v0.*.csv` (کوتیشن‌دار با `P1,P2,...`) + `v*_dataset.txt` + `v*_tagged.txt` + `"trace"` در `v*_detailed.json` | یک فایل واحد **`traces.tsv.gz`** با هدر شفاف و توکن‌های عددی جداشده با فاصله | حذف تکرار ۴ تا ۶ برابری + کاهش ۸۵ تا ۹۸٪ حجم روی دیسک |
| **نگاشت و متادیتای تست‌ها** | `tests.jsonl` + `excluded_tests.csv` + آرایه `tests` در `v*_detailed.json` | یک فایل **`tests.tsv`** شامل تمام تست‌های اجراشده و حذف‌شده | سبک، جدول‌محور و قابل باز کردن مستقیم در Excel / Pandas |
| **خلاصه نسخه** | `v*_detailed.json` (حجیم به‌خاطر متن trace) | **`summary.json`** (فقط آمار، شناسه خطاها و خلاصه کیفیت اوراکل) | سبک و سریع برای اسکریپت‌های گزارش‌گیری |
| **نسخه‌های میانی Siemens** | کپی‌برداری `inputs/` و کامپایل دوگانه در `source/` و `source.alt/` و `versions.alt/` | فقط یک نسخه کامپایل در `output/siemens/<repo>/orig/` و `v1..vN/` | کاهش بیش از ۵۰٪ فضای بیلد و زمان کامپایل |
| **فایل‌های خام Defects4J** | باقی‌ماندن `tests/*/trace.bin` و `checkouts/` بعد از اتمام | پاک‌سازی خودکار پس از ساخت `traces.tsv.gz` (مگر با `--keep-raw-traces` / `--keep-checkouts`) | جلوگیری از اشغال ده‌ها گیگابایت فضای اضافی |

> **نکته الگوریتمی مهم (چرا تکرارهای داخل مسیر حذف نشده‌اند؟):**
> موتور EFL روی **توالی دقیق و ترتیبی اجرا** N-gram می‌سازد و احتمال شرطی محاسبه می‌کند؛ بنابراین حذف تکرار حلقه‌ها (تبدیل به Set یا Coverage) نتیجه الگوریتم EFL را تغییر می‌دهد. فشرده‌سازی در فرمت جدید از طریق **Gzip روی جریان TSV** و **حذف تکرار بین فایل‌ها** انجام شده و کاملاً **بدون اتلاف (Lossless)** است.

---

## ۲. مدل اینسترومنتیشن در هر دو بنچ‌مارک

### الف) بنچ‌مارک Siemens (`siemens_instrumenter`)
- **ابزار و سطح اینسترومنتیشن:** تحلیلگر AST مبتنی بر Clang (`instrumenter.py`) در سطح **ساختارهای کنترلی و بلوک‌های پایه (Basic Block / Control-Flow AST Points)** روی کد C/C++ (مانند ورود تابع، `IfStmt`، `WhileStmt`، `ForStmt`، `DoStmt`، `SwitchStmt` و شاخه‌ها).
- **چگونگی تخصیص شناسه‌ها:** نقاط کشف‌شده در کد منبع اصلی (`orig`) به‌صورت قطعی مرتب شده و شماره‌های `1, 2, 3, ...` (معادل `P1, P2, P3, ...`) به آن‌ها داده می‌شود.
- **ساختار نسخه‌های کد روی دیسک:**
  - کدهای **اولیه و دست‌نخورده (Uninstrumented):**
    - نسخه سالم مرجع: `siemens_instrumenter/input/siemens/<repo>/source/<stem>.c` (یا `source.alt/source.orig/<stem>.c`)
    - نسخه‌های خطادار: `siemens_instrumenter/input/siemens/<repo>/versions.alt/versions.orig/v<id>/<stem>.c` (یا `source.alt/v<id>/<stem>.c`)
    - استخر تست و ورودی‌ها: `testplans.alt/universe` و `inputs/` در همان پوشه ورودی.
  - کدهای **اینسترومنت‌شده و باینری‌های کامپایل‌شده:**
    - نسخه سالم اینسترومنت‌شده: `siemens_instrumenter/output/siemens/<repo>/orig/<stem>.c` و `<stem>.exe`
    - نسخه خطادار اینسترومنت‌شده: `siemens_instrumenter/output/siemens/<repo>/v<id>/<stem>.c` و `<stem>.exe`
- **اوراکل pass/fail:** هر تست از `universe` هم روی باینری سالم `orig` و هم روی باینری خطادار `v<id>` اجرا می‌شود؛ در صورت تفاوت `stdout` یا `returncode` (یا timeout)، وضعیت تست `F` است.

### ب) بنچ‌مارک Defects4J (`defects4j_instrumenter`)
- **ابزار و سطح اینسترومنتیشن:** ایجنت جاوا (`java.lang.instrument` + کتابخانه **ASM**) در سطح **خطوط اجرایی بایت‌کد جاوا (`LineNumberNode` / `visitLineNumber`)**.
- **چگونگی تخصیص شناسه‌ها:** ابزار `ClassScanner` کلاس‌های کامپایل‌شده نسخه باگی (`b`) را بر اساس محدوده انتخاب‌شده (`--instrument-scope`: `relevant`، `modified` یا `all`) اسکن کرده و به‌صورت قطعی (بر اساس نام کلاس، متد، توصیف‌گر و شماره خط) شناسه عددی `1, 2, 3, ...` می‌سازد.
- **جمع‌آوری مسیر در زمان اجرا:** `EflTransformer` در هر خط شماره‌گذاری‌شده بایت‌کد، فراخوانی `TraceRuntime.hit(predicateId)` را تزریق می‌کند (بدون تغییر فایل‌های `.java` روی دیسک) و ترتیب دقیق اجرا در باینری موقت ثبت و در نهایت به `traces.tsv.gz` تبدیل می‌شود.
- **اوراکل pass/fail:** بر اساس خروجی `defects4j test` روی نسخه باگی (`v<id>b`)؛ اگر `failing_tests` غیرخالی باشد وضعیت `F` و اگر تست با موفقیت تمام شود `T` است.

---

## ۳. ساختار پوشه خروجی و معنی تمام فایل‌ها

هر نسخه تست‌شده (در هر دو پروژه) دقیقاً در این مسیر قرار می‌گیرد:
```text
results/<program>/v<id>/<test_set>/
├── traces.tsv.gz        # مسیرهای اجرای تمام تست‌های معتبر (ورودی اصلی EFL)
├── tests.tsv            # نگاشت test_id به نام/دستور دقیق تست و متادیتای اجرا
├── source_map.csv       # نگاشت شناسه عددی گره (id / P<id>) به فایل، خط و تابع در کد منبع
├── source_changes.json  # خطوط تغییریافته بین نسخه سالم (orig/fixed) و خطادار (buggy)
├── metadata.json        # مشخصات برنامه، نسخه و مسیر کدهای منبع
├── summary.json         # خلاصه آماری نسخه، شناسه‌های خطا و شمارش T / CC / F
└── cache_manifest.json  # اندازه و SHA-256 فایل‌ها برای کش و اعتبارسنجی EFL
```

### ۳.۱. فایل `traces.tsv.gz`
فایل متنی با جداکننده Tab (`\t`) و فشرده‌شده با Gzip:
```tsv
test_id	status	cc	path
T1	T	0	1 2 3 4 4 3 5
T2	F	0	1 2 4 6
T3	T	1	1 2 4 5
```
- **`test_id`**: شناسه یکتا و کوتاه تست (`T1`, `T2`, ...). این شناسه با ستون `test_id` در `tests.tsv` متناظر است (کوتاه بودن آن مانع خطای طول مسیر روی ویندوز در `fail_target_<name>` می‌شود).
- **`status`**: نتیجه سخت‌گیرانه (Strict Oracle) تست برای EFL و SBFL؛ فقط `T` (موفق) یا `F` (ناموفق).
- **`cc`**: برچسب حقیقت زمینه‌ای **Coincidental Correctness**:
  - `1`: تست پاس شده (`status=T`)، **اما** حداقل یکی از گره‌های خطا (`fault_predicates`) در مسیر `path` آن اجرا شده است.
  - `0`: در غیر این صورت (`status=F` یا تست سالم `T` بدون عبور از گره خطا).
- **`path`**: دنباله دقیق شناسه‌های عددی گره‌ها به ترتیب زمانی اجرا با جداکننده فاصله (` `).
  - هر عدد `k` دقیقاً معادل ستون `id` (و `P<k>` در ستون `predicate`) در فایل `source_map.csv` است.

**نحوه خواندن سریع با ابزارهای استاندارد:**
```bash
# مشاهده ۱۰ سطر اول در لینوکس/مک/WSL
gzip -dc traces.tsv.gz | head -n 10
```
```python
# خواندن مستقیم در پایتون / Pandas
import pandas as pd
df = pd.read_csv("traces.tsv.gz", sep="\t")
```

### ۳.۲. فایل `tests.tsv`
فایل جدول‌دار شامل **همه** تست‌ها (چه تست‌های واردشده به `traces.tsv.gz` و چه تست‌های حذف‌شده با prefix `X1, X2, ...`):
- **`test_id`**: `T1..TN` برای تست‌های موجود در `traces.tsv.gz`، یا `X1..XM` برای تست‌های حذف‌شده.
- **`test_name`**:
  - در **Siemens**: دستور دقیق آن سطر از فایل `testplans.alt/universe` (مثل `-t 5 < inputs/t12`) تا دقیقاً بدانید کدام ورودی باعث اجرای `T12` شده است.
  - در **Defects4J**: نام کامل متد تست JUnit (مثل `org.apache.commons.lang3.math.NumberUtilsTest#testCreateNumber`).
- **`status`**: `T` (موفق)، `F` (ناموفق)، یا `U` (نامشخص / خطا / timeout).
- **`cc`**: `1` (تصادفاً موفق بر اساس ground truth) یا `0`.
- **`included`**: `1` اگر در `traces.tsv.gz` آمده است؛ `0` اگر حذف شده است.
- **`trace_len`**: تعداد کل رویدادهای ثبت‌شده در مسیر اجرای تست.
- **`duration_sec`**, **`return_code`**, **`timed_out`**, **`reason`**: زمان اجرا، کد خروج، وضعیت timeout و دلیل حذف در صورت `included=0` (مثلاً `empty_trace` یا `invalid_or_fixed_failure`).

### ۳.۳. فایل `source_map.csv`
جدول نگاشت هر نقطه اینسترومنتیشن به کد منبع:
- ستون‌های مشترک:
  - `predicate`: نام نمادین قدیمی (`P1`, `P2`, ...)
  - `id`: شناسه عددی (`1`, `2`, ...) — **همان عددی که در `traces.tsv.gz` می‌آید**
  - `file`: مسیر/نام فایل سورس مبنا
  - `line`: شماره خط در کد منبع
  - `column`: شماره ستون (در Siemens دقیق از AST؛ در Defects4J مقدار `0` چون سطح خط بایت‌کد است)
  - `function`: نام تابع (C) یا `Class#method` (Java)
- ستون‌های تکمیلی:
  - در Siemens: `kind` (نوع گره AST مثل `IfStmt`، `WhileStmt`) و `address`
  - در Defects4J: `class`, `method`, `descriptor`, `event_type` (`line`)

### ۳.۴. فایل `source_changes.json`
فهرست بلاک‌های تغییریافته بین نسخه سالم و خطادار:
- `file`: فایل منبع تغییریافته
- `kind` / `change_type`: نوع تغییر (`replace`, `delete`, `insert`, `replacement`)
- `reference_lines`: شماره خطوط در نسخه سالم (`orig` یا `fixed`)
- `faulty_lines`: شماره خطوط متناظر در نسخه خطادار (`v<id>` یا `buggy`)
- `reference_text` / `faulty_text`: متن خطوط قبل و بعد از باگ

### ۳.۵. فایل‌های `summary.json`، `metadata.json` و `cache_manifest.json`
- **`summary.json`**: شامل نسخه اسکیما (`efl-dataset-v2`)، نوع اینسترومنتیشن، تعداد تست‌های کشف/شامل/حذف‌شده، تعداد `pass_clean`، `coincidental_correct`، `fail` و لیست `fault_predicates` / `fault_predicate_ids`.
- **`metadata.json`**: نام بنچ‌مارک، برنامه، شماره نسخه، `test_set` و مسیرهای سورس مبنا (و در Defects4J کلاس‌ها و تست‌های relevant/trigger).
- **`cache_manifest.json`**: اندازه و هش `SHA-256` فایل‌های خروجی برای بررسی سلامت و کش EFL.

---

## ۴. راهنمای عملی: تطبیق دیتاست با کد منبع و تست‌ها

فرض کنید در خروجی EFL یا `traces.tsv.gz` به **تست `T15`** و **گره `42` (`P42`)** برخورد کرده‌اید و می‌خواهید آن را در کد منبع اصلی پیدا کنید:

1. **پیدا کردن تست‌کیس اصلی:**
   - در فایل `tests.tsv` سطر با `test_id = T15` را ببینید.
   - ستون `test_name` در Siemens خط دقیق `universe` (و نام فایل ورودی در `inputs/`) را می‌دهد و در Defects4J نام کامل متد JUnit (`Class#method`) را می‌دهد.
   - اگر بخواهید همان تست را دستی روی نسخه اینسترومنت‌شده Siemens اجرا کنید، باینری آن در `output/siemens/<repo>/v<id>/<stem>.exe` (و نسخه سالم در `orig/<stem>.exe`) قرار دارد.
2. **پیدا کردن محل دقیق در کد منبع:**
   - در `source_map.csv` سطر با `id = 42` (`predicate = P42`) را پیدا کنید.
   - ستون‌های `file`، `line`، `column` و `function` مکان دقیق را در کد منبع اصلی (`input/siemens/<repo>/...` یا چک‌اوت Defects4J برای `<Project>-<id>b`) مشخص می‌کنند.
3. **پیدا کردن محل باگ و مقایسه با نسخه سالم:**
   - فایل `source_changes.json` را ببینید تا خطوط دقیق `faulty_lines` (در نسخه خطادار) و `reference_lines` (در نسخه سالم) را به همراه متن تغییر ببینید.
   - فایل `summary.json` در فیلد `fault_predicate_ids` مشخص می‌کند کدام `id`ها روی خطوط خطا افتاده‌اند (و مبنای محاسبه `cc=1` در `traces.tsv.gz` بوده‌اند).
