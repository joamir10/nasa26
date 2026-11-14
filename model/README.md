# Planet X — الموقع + الباك اند

سيرفر واحد (Flask + SQLite) بيخدّم الموقع (`public/`) والـ API على نفس الدومين، فمفيش CORS ولا إعدادات معقدة.

## تشغيل محلي

```bash
pip install -r requirements.txt
python app.py            # http://localhost:8000
python -m unittest discover -s tests -v     # اختبارات الـ API (16)
python tests/e2e_browser.py                 # اختبار متصفح حقيقي (محتاج playwright)
```

## اللي اتضاف

| الجزء | التفاصيل |
|---|---|
| تسجيل دخول حقيقي | باسورد متشفّر (scrypt)، جلسة بكوكي `HttpOnly` + `SameSite=Lax`، حد لمحاولات الدخول، فحص `Origin` ضد CSRF |
| قاعدة بيانات | SQLite: `users, sessions, candidates, favorites, articles, analyses` (الـ schema أول `app.py`) |
| المرشحين (candidates) | بيتحفظوا على حساب المستخدم ويتزامنوا بين الأجهزة. اللي اتحفظ قبل تسجيل الدخول بيترفع تلقائيًا بعده |
| الحقول المفضلة | زر **Save field** في تاب View، وقايمة في صفحة Sign in |
| المقالات | بتتحمّل من قاعدة البيانات (`seed_articles.json`)، ولو السيرفر مش موجود الموقع بيستخدم النسخة المدمجة |
| الموديل | تاب Discover فيه زر **Run tracking model** بيبعت للـ API ويعرض المسارات (انظر تحت) |
| وضع offline | لو الباك اند مش متاح الموقع بيشتغل محليًا بنفس السلوك القديم (مكتوب للمستخدم صراحةً) |

إصلاحات في الكود الأصلي: مسار SVG ناقص في كارت Hall 02 (كان بيطلّع error في الكونسول)، وسم `</main>` مكرر، وملاحظة placeholder ظاهرة للزوار ("Aladin Lite goes in…")، وتهريب (escape) محتوى المقالات.

## الـ API

| Method | Path | ملاحظات |
|---|---|---|
| GET | `/api/health` | حالة السيرفر + معلومات الموديل |
| POST | `/api/auth/register` `/login` `/logout` | `{name,email,password,remember}` |
| GET | `/api/auth/me` | `{user}` أو `null` |
| GET/POST | `/api/candidates` | تحتاج دخول. `DELETE /api/candidates/<id>`، `POST /api/candidates/import` |
| GET/POST | `/api/favorites` | تحتاج دخول. `DELETE /api/favorites/<id>` |
| GET | `/api/articles?category=` | |
| GET | `/api/model` | معلومات الموديل المتصل |
| POST | `/api/analyze` | تحليل حقل (متاح من غير دخول، محدود 30/دقيقة/IP) |

## ربط الموديل بتاعك

كل الكلام مع الموديل بيعدّي من `model_adapter.py`. اختار بـ `MODEL_BACKEND`:

1. **`builtin`** (الافتراضي): تراكر فيزيائي بيربط المصادر عبر كل الـ epochs بفرضية حركة خطية ويطلّع المتحركات، من غير ML. ده baseline شغّال وبيكتشف المتحركات في الاختبارات حتى مع ضوضاء.
2. **`custom`**: انسخ `custom_model.py.example` لـ `custom_model.py` واكتب `predict(payload)`. بتحمّل فيه ONNX أو PyTorch أو أي حاجة. (لو هتستخدم onnxruntime أو torch ضيفهم في `requirements.txt`.)
3. **`http`**: موديلك سيرفس لوحده؟ حط `MODEL_URL` (و`MODEL_API_KEY` لو محتاج) والباك اند هيبعتله نفس الـ payload كـ JSON.

**المدخل** (`POST /api/analyze`):
```json
{"ra": 133.79, "dec": -7.24, "fov": 240,
 "sources": [{"epoch": 2025.35, "x": 12.3, "y": -4.5, "flux": 0.4}, ...]}
```
`x` بالثواني القوسية ناحية الغرب (RA بيقل)، `y` ناحية الشمال. اختياري: `tol, max_pm, min_pm, min_snr, ref_epoch`.

**المخرج المطلوب من أي موديل:**
```json
{"tracks": [{"id": "M1", "ra": 133.79, "dec": -7.24, "pm_arcsec_yr": 8.06, "pa_deg": 240,
             "score": 0.99}], "n_static": 120}
```
الواجهة بتعرض `id, ra, dec, pm_arcsec_yr, pa_deg, n_epochs, n_total_epochs, pm_snr` لو موجودين.

> **مهم:** الـ payload الحالي قوائم مصادر (detections) مش صور. لو موديلك بياخد صور أو cutouts، الباك اند محتاج endpoint يجيبها (من IRSA) قبل ما يبعتها للموديل. ده مش مضاف لسه (انظر الخطوات الجاية).

## النشر

**Docker** (أي استضافة بتدعمه):
```bash
docker build -t planetx . && docker run -p 8000:8000 -v planetx-data:/app/data -e COOKIE_SECURE=0 planetx
```
على استضافة HTTPS ضبط `COOKIE_SECURE=1` و`TRUST_PROXY=1`. فيه `render.yaml` و`Procfile` جاهزين (Render/Railway/Heroku-style).

**⚠ التخزين:** SQLite ملف على الـ disk. الاستضافات المجانية غالبًا disk مؤقت، يعني المستخدمين والمرشحين **هيتمسحوا مع كل نشر**. استخدم disk/volume دائم، أو انقل لـ Postgres لو عايز تكبّر (السكيما بسيطة).

## متغيرات البيئة
انظر `.env.example`.

## قبل التقديم
- [ ] ضيف صور الفريق `member1.png … member6.png` جوه `public/` (الموقع بيعرض silhouette لحد ما تحطهم).
- [ ] جرّب الموقع كله على الرابط المنشور من موبايل وكمبيوتر، وجرّب تسجيل حساب ومرشح ومفضلة.
- [ ] تأكد إن الـ disk دائم (أو اعمل نسخة احتياطية من `data/planetx.db`).
- [ ] صور المقالات من Unsplash وخطوط Google بتتحمّل من النت؛ لو العرض أمام لجنة بدون إنترنت، نزّلهم محليًا.

## الخطوات الجاية (مش متعمّلة)
- جلب cutouts حقيقية من IRSA/SPHEREx بدل الصور المحاكاة (البيكسلات حاليًا محاكاة في المتصفح، والأسماء والمواضع والتواريخ حقيقية).
- مطابقة Gaia/WISE فعلية (حاليًا الكتالوج محاكى).
- Postgres + حذف الحساب/استرجاع الباسورد لو هتتحول لمنتج حقيقي.

## موديل الـ ML (Tracklet scorer)

`MODEL_BACKEND=custom` بيشغّل `custom_model.py`: التراكر الفيزيائي بيطلّع **مرشحين** (tracklets) بإعدادات متساهلة، وشبكة عصبية صغيرة (MLP، inference بـ numpy بس، الأوزان في `models/tracklet_mlp.json`) بتقرر إيه الحقيقي. كل track فيه `ml_prob`.

```bash
pip install -r requirements-train.txt && python ml/train.py   # بيعيد التدريب ويكتب models/tracklet_mlp.json و eval.json
python -m unittest discover -s tests -v                        # 21 اختبار (منهم tests/test_ml.py)
```

**التدريب والتقييم على محاكاة مش داتا حقيقية.** المحاكاة: نجوم ثابتة + 0-3 متحركين + ضوضاء موضع + مصادر باهتة بتتفوّت + كشوف كاذبة. التقييم على حقول جديدة (seeds مختلفة). دقة / استرجاع / إنذارات كاذبة (ML مقابل القاعدة القديمة):

| ضوضاء الموضع | ML (P / R / FP) | قاعدة (P / R / FP) | سقف الاسترجاع (مولّد المرشحين) |
|---|---|---|---|
| σ=0.5″ | 0.49 / 0.60 / 63 | 0.30 / 0.62 / 147 | 0.63 |
| σ=1.2″ | 0.61 / 0.40 / 22 | 0.32 / 0.45 / 85 | 0.48 |
| σ=2.5″ | 0.50 / 0.01 / 1 | 0.16 / 0.11 / 43 | 0.11 |
| σ=5.0″ | 0.00 / 0.00 / 0 | 0.12 / 0.01 / 7 | 0.01 |

القراءة الصادقة: الـ ML بيقلّل الإنذارات الكاذبة 2-4 أضعاف مع خسارة بسيطة في الاسترجاع لما الضوضاء قليلة/متوسطة. **عند ضوضاء عالية (σ ≥ 2.5″) مولّد المرشحين نفسه هو العنق (السقف ≈ 0.11)**، والـ ML بيبقى متحفظ جدًا وبيخسر الاسترجاع القليل اللي كان موجود، فاستخدم `ML_THRESHOLD` أقل أو `MODEL_BACKEND=builtin` هناك. **ما اتجرّبش على كشوف SPHEREx حقيقية.**
