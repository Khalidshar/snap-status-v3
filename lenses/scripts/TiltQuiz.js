// صح أو خطأ | روّق — كتبه Gemini، راجعته شيهانة (١٣ نوفمبر ٢٠٢٦). صقل ١٤ نوفمبر: عدّاد، ردّ فعل الميل، حركات، شاشة نهاية، إعادة لعب، حماية الوجه.
// @input Component.Image displayImage
// @input Component.Image badgeImage
// @input Component.Text scoreText
// @input Asset.Texture[] questionTextures
// @input Asset.Texture[] answerTextures
// @input bool[] isTrueAnswers
// @input Asset.Texture badgeCorrect
// @input Asset.Texture badgeIncorrect
// @input SceneObject headTracker
// @input bool invertTilt = false
// @input float tiltAngleThreshold = 12.0
// @input float neutralAngleThreshold = 5.0
// @input float resultDuration = 1.5
// @input SceneObject[] hideOnDone // عناصر تُخفى عند ظهور النتيجة (إشارتا صح/خطأ)
// @input SceneObject hintRight {"hint":"اختياري — وإلا hideOnDone[0]"}
// @input SceneObject hintLeft {"hint":"اختياري — وإلا hideOnDone[1]"}
// @input Component.Camera faceCamera {"hint":"اختياري — وإلا الكاميرا الأب لـ headTracker"}

var currentState = "START";
var indices = [];
var currentStep = 0;
var score = 0;
// العتبات كجيب زاوية: نقارن بها المركّبة X لمتجه «فوق» الرأس
var tiltSin = Math.sin(script.tiltAngleThreshold * (Math.PI / 180));
var neutralSin = Math.sin(script.neutralAngleThreshold * (Math.PI / 180));

// ---------- حالة إضافية (لا تمسّ منطق الميل) ----------
var faceVisible = false;
var pendingNext = false;   // انتهى وقت عرض الإجابة والوجه غائب: ننتظر عودته
var fbTilt = 0;            // آخر ميل محسوب، لردّ الفعل البصري فقط

var delayEvent = script.createEvent("DelayedCallbackEvent");
delayEvent.bind(function () {
    if (!faceVisible) { pendingNext = true; return; }
    nextQuestion();
});

// ---------- أدوات ----------
function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
function lerp(a, b, t) { return a + (b - a) * t; }
function easeOut(t) { t = clamp(t, 0, 1); return 1 - (1 - t) * (1 - t) * (1 - t); }
function arabicDigits(n) {
    var d = "٠١٢٣٤٥٦٧٨٩", s = String(n), out = "";
    for (var i = 0; i < s.length; i++) {
        var c = s.charCodeAt(i) - 48;
        out += (c >= 0 && c <= 9) ? d.charAt(c) : s.charAt(i);
    }
    return out;
}
function st(obj) { return obj ? obj.getComponent("Component.ScreenTransform") : null; }

var cardObj = script.displayImage.getSceneObject();
var cardST = st(cardObj);
var regionObj = cardObj.getParent();
var cardBaseColor = script.displayImage.mainPass.baseColor;
var badgeST = st(script.badgeImage.getSceneObject());

// ---------- إشارتا صح/خطأ: محاذاة الأذنين + ردّ فعل الميل ----------
function makeHint(obj) {
    if (!obj) { return null; }
    var t = st(obj);
    var img = obj.getComponent("Component.Image");
    if (!t || !img) { return null; }
    var a = t.anchors;
    return {
        obj: obj, st: t, img: img,
        left: a.left, right: a.right, half: (a.top - a.bottom) / 2,
        base: img.mainPass.baseColor,
        s: 1, glow: 0, fade: 0
    };
}
var hintR = makeHint(script.hintRight || (script.hideOnDone && script.hideOnDone[0]));
var hintL = makeHint(script.hintLeft || (script.hideOnDone && script.hideOnDone[1]));
var HINT_Y_MIN = -0.55, HINT_Y_MAX = 0.22; // داخل Safe Render، فوق أزرار سناب وتحت العدّاد
var HINT_EAR_OFFSET = 0.1;
var hintY = (hintR ? (hintR.st.anchors.top + hintR.st.anchors.bottom) / 2 : -0.3);

function findCamera() {
    if (script.faceCamera) { return script.faceCamera; }
    var o = script.headTracker;
    while (o) {
        var c = o.getComponent("Component.Camera");
        if (c) { return c; }
        o = o.getParent();
    }
    return null;
}
var faceCam = findCamera();

function placeHint(h, y) {
    if (!h) { return; }
    h.st.anchors = Rect.create(h.left, h.right, y - h.half, y + h.half);
    h.st.scale = new vec3(h.s, h.s, 1);
    var b = h.base;
    // المادة بيضاء (1,1,1,1) فلا يمكن تجاوزها: الراحة 0.9 والتوهّج يرفعها إلى 1
    // الإشارة الأخرى تبهت وتبقى مقروءة (الشفافية في هذه المادة تُطبَّق بقوة، فنعتمد أكثر على التعتيم)
    var a = 1 - 0.2 * h.fade;
    var k = 0.9 + 0.1 * h.glow - 0.3 * h.fade;
    h.img.mainPass.baseColor = new vec4(b.r * k, b.g * k, b.b * k, b.a * a);
}

// ---------- عدّاد التقدّم ----------
function makeText(name, anchors, size, color) {
    var so = global.scene.createSceneObject(name);
    so.setParent(regionObj);
    so.layer = regionObj.layer;
    var t = so.createComponent("Component.ScreenTransform");
    t.anchors = anchors;
    t.offsets = Rect.create(0, 0, 0, 0);
    var tx = so.createComponent("Component.Text");
    if (script.scoreText) { tx.font = script.scoreText.font; }
    tx.size = size;
    tx.textFill.color = color;
    tx.horizontalAlignment = HorizontalAlignment.Center;
    tx.verticalAlignment = VerticalAlignment.Center;
    tx.horizontalOverflow = HorizontalOverflow.Overflow;
    tx.verticalOverflow = VerticalOverflow.Overflow;
    tx.outlineSettings.enabled = true;
    tx.outlineSettings.fill.color = new vec4(0.05, 0.2, 0.14, 1);
    tx.outlineSettings.size = 0.18;
    return tx;
}
// تصغير بطاقة السؤال (طلب خالد: «مستطيل السؤال كبير») مع إبقاء أعلاها في مكانه
var CARD_SCALE = 0.8;
var cardST = st(cardObj);
var cardBottom = 0.38, cardLeft = -0.8, cardTop = 0.9;
if (cardST) {
    var a0 = cardST.anchors;
    var cx = (a0.left + a0.right) / 2, hw = (a0.right - a0.left) / 2 * CARD_SCALE;
    var h = (a0.top - a0.bottom) * CARD_SCALE;
    cardST.anchors = Rect.create(cx - hw, cx + hw, a0.top - h, a0.top);
    cardBottom = a0.top - h;
    cardLeft = cx - hw; cardTop = a0.top;
}
var GOLD = script.scoreText ? script.scoreText.textFill.color : new vec4(0.83, 0.69, 0.22, 1);
// العدّاد داخل بطاقة السؤال في زاويتها العليا اليسرى: صغير وذهبي، لا يغطي الوجه
var counterText = makeText("Progress Counter", Rect.create(cardLeft + 0.07, cardLeft + 0.37, cardTop - 0.115, cardTop - 0.055), 22, GOLD);
counterText.outlineSettings.enabled = false;
var counterST = counterText.getSceneObject().getComponent("Component.ScreenTransform");
var counterPulse = 0;

// ---------- شاشة النهاية ----------
var BG_MARGIN_X = 40, BG_MARGIN_TOP = 25, BG_MARGIN_BOTTOM = 135, BG_RADIUS = 24;
var titleText = makeText("Score Title", Rect.create(-0.9, 0.9, 0.40, 0.54), 44, GOLD);
titleText.getSceneObject().enabled = false;
var scoreST = script.scoreText ? st(script.scoreText.getSceneObject()) : null;
if (scoreST) { scoreST.anchors = Rect.create(-0.9, 0.9, 0.56, 0.78); }
if (script.scoreText) {
    // النتيجة بالأبيض، واللقب بالذهبي، وبطاقة خضراء خلفهما (تمتد للأسفل لتشمل اللقب)
    script.scoreText.textFill.color = new vec4(1, 1, 1, 1);
    var bg = script.scoreText.backgroundSettings;
    bg.enabled = true;
    bg.fill.color = new vec4(0.03, 0.22, 0.15, 0.92);
    bg.margins = Rect.create(BG_MARGIN_X, BG_MARGIN_X, BG_MARGIN_BOTTOM, BG_MARGIN_TOP);
    bg.cornerRadius = BG_RADIUS;
}

// ---------- الحركات ----------
var anims = [];
function animate(dur, fn) {
    for (var i = anims.length - 1; i >= 0; i--) { if (anims[i].fn === fn) { anims.splice(i, 1); } }
    anims.push({ t: 0, dur: dur, fn: fn });
    fn(0);
}
function cardIntro(p) {
    var e = easeOut(p), s = lerp(0.92, 1, e);
    cardST.scale = new vec3(s, s, 1);
    var c = cardBaseColor;
    script.displayImage.mainPass.baseColor = new vec4(c.r, c.g, c.b, c.a * e);
}
function badgePulse(p) {
    var s = p < 0.45 ? lerp(0.6, 1.25, easeOut(p / 0.45)) : lerp(1.25, 1, easeOut((p - 0.45) / 0.55));
    if (badgeST) { badgeST.scale = new vec3(s, s, 1); }
}
function endIntro(p) {
    var e = easeOut(p), s = lerp(0.8, 1, e);
    if (scoreST) { scoreST.scale = new vec3(s, s, 1); }
    var tst = st(titleText.getSceneObject());
    var s2 = lerp(0.6, 1, easeOut(clamp(p * 1.4 - 0.4, 0, 1)));
    tst.scale = new vec3(s2, s2, 1);
}

// ---------- اللعب ----------
function startRound() {
    if (script.questionTextures.length !== script.answerTextures.length ||
        script.questionTextures.length !== script.isTrueAnswers.length) {
        print("خطأ: يجب أن تتطابق أعداد الأسئلة وبطاقات الإجابات والخيارات المنطقية.");
        return false;
    }
    indices = [];
    for (var i = 0; i < script.questionTextures.length; i++) { indices.push(i); }
    shuffleIndices(indices);
    currentStep = 0;
    score = 0;
    pendingNext = false;
    if (indices.length === 0) { return false; }
    script.displayImage.enabled = true;
    script.badgeImage.enabled = false;
    if (script.hideOnDone) {
        for (var h = 0; h < script.hideOnDone.length; h++) {
            if (script.hideOnDone[h]) { script.hideOnDone[h].enabled = true; }
        }
    }
    if (script.scoreText) { script.scoreText.enabled = false; }
    titleText.getSceneObject().enabled = false;
    counterText.getSceneObject().enabled = true;
    showCurrentQuestion();
    return true;
}

function initGame() {
    if (startRound()) { currentState = "WAITING_FOR_TILT"; }
}

function shuffleIndices(array) {
    for (var i = array.length - 1; i > 0; i--) {
        var j = Math.floor(Math.random() * (i + 1));
        var temp = array[i]; array[i] = array[j]; array[j] = temp;
    }
}

function showCurrentQuestion() {
    script.displayImage.mainPass.baseTex = script.questionTextures[indices[currentStep]];
    counterText.text = arabicDigits(currentStep + 1) + " من " + arabicDigits(indices.length);
    counterPulse = 1;
    animate(0.25, cardIntro);
}

// لقب النتيجة: ٠–٩ / ١٠–١٥ / ١٦–٢٠
function scoreTitle(n) {
    if (n >= 16) { return "موسوعة متنقلة"; }
    if (n >= 10) { return "مثقف المجلس"; }
    return "مُحب للمعرفة";
}

function nextQuestion() {
    pendingNext = false;
    currentStep++;
    script.badgeImage.enabled = false;
    if (currentStep < indices.length) {
        showCurrentQuestion();
        currentState = "WAITING_FOR_NEUTRAL";
    } else {
        script.displayImage.enabled = false;
        counterText.getSceneObject().enabled = false;
        if (script.hideOnDone) {
            for (var h = 0; h < script.hideOnDone.length; h++) {
                if (script.hideOnDone[h]) { script.hideOnDone[h].enabled = false; }
            }
        }
        if (script.scoreText) {
            script.scoreText.enabled = true;
            script.scoreText.text = "نتيجتك: " + arabicDigits(score) + " من " + arabicDigits(indices.length);
        }
        titleText.text = scoreTitle(score);
        titleText.getSceneObject().enabled = true;
        animate(0.45, endIntro);
        currentState = "DONE";
    }
}

// إعادة اللعب: نقرة بعد النهاية
script.createEvent("TapEvent").bind(function () {
    if (currentState !== "DONE") { return; }
    if (startRound()) {
        neutralWait = 0;
        currentState = "WAITING_FOR_NEUTRAL";
    }
});

// حماية: لا ميل ولا تقدّم والوجه غائب
var faceFound = script.createEvent("FaceFoundEvent");
faceFound.faceIndex = 0;
faceFound.bind(function () {
    faceVisible = true;
    // عاد الوجه وقد يكون مائلًا: ننتظر رجوعه للوسط قبل قبول إجابة
    if (currentState === "WAITING_FOR_TILT" && calibTime >= 1.0) {
        neutralWait = 0;
        currentState = "WAITING_FOR_NEUTRAL";
    }
    if (pendingNext) { nextQuestion(); }
});
var faceLost = script.createEvent("FaceLostEvent");
faceLost.faceIndex = 0;
faceLost.bind(function () { faceVisible = false; fbTilt = 0; });

var baseX = 0;
var calibTime = 0;
var calibSum = 0;
var calibN = 0;
var neutralWait = 0;
var updateEvent = script.createEvent("UpdateEvent");
updateEvent.bind(function () {
    if (!script.headTracker) { return; }
    if (!faceVisible) { return; }
    // الميل من متجه «فوق» الرأس في إحداثيات العالم، لا من زوايا أويلر
    // (تفكيك أويلر غير ثابت على الجهاز حين يكون انعطاف الرأس قرابة 180°).
    // up.x موجب = أعلى الرأس مال نحو يمين الشاشة كما يراه المستخدم (جهة «صح») — مُختبر في المعاينة.
    var up = script.headTracker.getTransform().getWorldRotation().multiplyVec3(vec3.up());
    var rawX = up.x;
    // معايرة: وضع الرأس الطبيعي ليس صفرًا دائمًا (طريقة مسك الجوال وتتبّع الوجه)،
    // فنأخذ متوسط أول ثانية كنقطة الوسط، ثم نتابع انزياحها ببطء ما دام الرأس قريبًا منها.
    if (calibTime < 1.0) {
        if (rawX === 0) { return; } // الوجه لم يُكتشف بعد
        calibTime += getDeltaTime(); calibSum += rawX; calibN++;
        baseX = calibSum / calibN;
        return;
    }
    var tiltX = rawX - baseX;
    fbTilt = tiltX;
    if (Math.abs(tiltX) <= neutralSin) { baseX += (rawX - baseX) * 0.02; }
    if (currentState === "WAITING_FOR_NEUTRAL") {
        neutralWait += getDeltaTime();
        // إن بقي الرأس ثابتًا خارج الوسط ثانيتين، نعتبر وضعه الحالي هو الوسط
        // (فقط إن لم يكن مائلًا ميلة إجابة كاملة، حتى لا يُحسب رجوعه للوسط إجابة عكسية)
        var settled = neutralWait > 2.0 && Math.abs(tiltX) < tiltSin;
        if (Math.abs(tiltX) <= neutralSin || settled) {
            if (settled) { baseX = rawX; }
            neutralWait = 0;
            currentState = "WAITING_FOR_TILT";
        }
        return;
    }
    if (currentState === "WAITING_FOR_TILT") {
        var right = tiltX > tiltSin;
        var left = tiltX < -tiltSin;
        if (script.invertTilt) { var t = right; right = left; left = t; }
        var userAnswer = right ? true : (left ? false : null);
        if (userAnswer !== null) {
            var idx = indices[currentStep];
            script.displayImage.mainPass.baseTex = script.answerTextures[idx];
            script.badgeImage.enabled = true;
            if (userAnswer === script.isTrueAnswers[idx]) {
                script.badgeImage.mainPass.baseTex = script.badgeCorrect;
                score++;
            } else {
                script.badgeImage.mainPass.baseTex = script.badgeIncorrect;
            }
            animate(0.35, badgePulse);
            currentState = "SHOWING_RESULT";
            delayEvent.reset(script.resultDuration);
        }
    }
});

// ---------- تحديث بصري منفصل (لا يؤثر في الإجابة) ----------
var visualEvent = script.createEvent("UpdateEvent");
visualEvent.bind(function () {
    var dt = getDeltaTime();
    for (var i = anims.length - 1; i >= 0; i--) {
        var a = anims[i];
        a.t += dt;
        var p = clamp(a.t / a.dur, 0, 1);
        a.fn(p);
        if (p >= 1) { anims.splice(i, 1); }
    }
    if (!hintR && !hintL) { return; }
    // الارتفاع يتبع مستوى الأذنين (مركز الرأس) على الشاشة، والأفقي ثابت عند الحافتين
    if (faceVisible && faceCam && script.headTracker) {
        var sp = faceCam.worldSpaceToScreenSpace(script.headTracker.getTransform().getWorldPosition());
        var pp = (hintR || hintL).st.screenPointToParentPoint(sp);
        // مركز الرأس أخفض قليلًا من منتصف الأذن على الشاشة، فنرفعه HINT_EAR_OFFSET
        if (pp) { hintY = lerp(hintY, clamp(pp.y + HINT_EAR_OFFSET, HINT_Y_MIN, HINT_Y_MAX), clamp(dt * 6, 0, 1)); }
    }
    // ردّ فعل الميل: الجهة المائل إليها تكبر وتتوهّج، والأخرى تبهت
    var p2 = 0, sideRight = true;
    if (currentState === "WAITING_FOR_TILT" && faceVisible) {
        p2 = clamp(Math.abs(fbTilt) / tiltSin, 0, 1);
        sideRight = fbTilt > 0;
        if (script.invertTilt) { sideRight = !sideRight; }
    }
    var k = clamp(dt * 10, 0, 1);
    var hs = [hintR, hintL];
    for (var j = 0; j < 2; j++) {
        var h = hs[j];
        if (!h) { continue; }
        var active = (j === 0) === sideRight;
        h.s = lerp(h.s, active ? 1 + 0.3 * p2 : 1 - 0.08 * p2, k);
        h.glow = lerp(h.glow, active ? p2 : 0, k);
        h.fade = lerp(h.fade, active ? 0 : p2, k);
        placeHint(h, hintY);
    }
});


// نبضة العدّاد
script.createEvent("UpdateEvent").bind(function () {
    if (counterPulse <= 0 || !counterST) { return; }
    counterPulse = Math.max(0, counterPulse - getDeltaTime() * 4);
    var k = 1 + 0.08 * Math.sin(counterPulse * Math.PI);
    counterST.scale = new vec3(k, k, 1);
});

initGame();
