// صح أو خطأ | روّق — كتبه Gemini، راجعته شيهانة (١٣ نوفمبر ٢٠٢٦). يحتاج تجربة فعلية في Lens Studio.
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
// @input float tiltAngleThreshold = 15.0
// @input float neutralAngleThreshold = 5.0
// @input float resultDuration = 1.5
// @input SceneObject[] hideOnDone // عناصر تُخفى عند ظهور النتيجة (إشارتا صح/خطأ)

var currentState = "START";
var indices = [];
var currentStep = 0;
var score = 0;
// العتبات كجيب زاوية: نقارن بها المركّبة X لمتجه «فوق» الرأس
var tiltSin = Math.sin(script.tiltAngleThreshold * (Math.PI / 180));
var neutralSin = Math.sin(script.neutralAngleThreshold * (Math.PI / 180));

var delayEvent = script.createEvent("DelayedCallbackEvent");
delayEvent.bind(function () { nextQuestion(); });

function initGame() {
    if (script.questionTextures.length !== script.answerTextures.length ||
        script.questionTextures.length !== script.isTrueAnswers.length) {
        print("خطأ: يجب أن تتطابق أعداد الأسئلة وبطاقات الإجابات والخيارات المنطقية.");
        return;
    }
    for (var i = 0; i < script.questionTextures.length; i++) { indices.push(i); }
    shuffleIndices(indices);
    if (indices.length > 0) {
        showCurrentQuestion();
        script.badgeImage.enabled = false;
        if (script.scoreText) { script.scoreText.enabled = false; }
        currentState = "WAITING_FOR_TILT";
    }
}

function shuffleIndices(array) {
    for (var i = array.length - 1; i > 0; i--) {
        var j = Math.floor(Math.random() * (i + 1));
        var temp = array[i]; array[i] = array[j]; array[j] = temp;
    }
}

function showCurrentQuestion() {
    script.displayImage.mainPass.baseTex = script.questionTextures[indices[currentStep]];
}

function nextQuestion() {
    currentStep++;
    script.badgeImage.enabled = false;
    if (currentStep < indices.length) {
        showCurrentQuestion();
        currentState = "WAITING_FOR_NEUTRAL";
    } else {
        script.displayImage.enabled = false;
        if (script.hideOnDone) {
            for (var h = 0; h < script.hideOnDone.length; h++) {
                if (script.hideOnDone[h]) { script.hideOnDone[h].enabled = false; }
            }
        }
        if (script.scoreText) {
            script.scoreText.enabled = true;
            script.scoreText.text = "نتيجتك: " + score + " من " + indices.length;
        }
        currentState = "DONE";
    }
}

var baseX = 0;
var calibTime = 0;
var calibSum = 0;
var calibN = 0;
var neutralWait = 0;
var updateEvent = script.createEvent("UpdateEvent");
updateEvent.bind(function () {
    if (!script.headTracker) { return; }
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
            currentState = "SHOWING_RESULT";
            delayEvent.reset(script.resultDuration);
        }
    }
});

initGame();
