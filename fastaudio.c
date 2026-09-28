/*
 * fastaudio — C hot paths for the music bot.
 *
 * scale_pcm:   applies volume/loudness gain to a 20 ms frame of 16-bit
 *              little-endian PCM, with a soft limiter so boosted peaks are
 *              rounded off instead of hard-clipped. Called ~50 times a second
 *              per playing guild, so it stays out of Python entirely and
 *              releases the GIL while it works.
 * match_score: token-overlap similarity between two titles, used to pick the
 *              YouTube upload that best matches a Spotify track.
 *
 * Build: python3 setup.py build_ext --inplace
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <string.h>

/* Soft limiter: samples below the knee (-2.5 dBFS) pass through unchanged;
 * above it they're squeezed along a tanh curve that approaches, but never
 * reaches, full scale. */
#define LIMIT_KNEE 24575
#define LIMIT_RANGE (32767.0 - LIMIT_KNEE)

#define MAX_TOKENS 64
#define MAX_TOKEN_LEN 48

static PyObject *scale_pcm(PyObject *self, PyObject *args) {
    Py_buffer in;
    double volume;

    if (!PyArg_ParseTuple(args, "y*d", &in, &volume))
        return NULL;

    if (in.len % 2 != 0) {
        PyBuffer_Release(&in);
        PyErr_SetString(PyExc_ValueError, "PCM data must be 16-bit samples (even byte length)");
        return NULL;
    }

    PyObject *out = PyBytes_FromStringAndSize(NULL, in.len);
    if (!out) {
        PyBuffer_Release(&in);
        return NULL;
    }

    const uint8_t *src = (const uint8_t *)in.buf;
    uint8_t *dst = (uint8_t *)PyBytes_AS_STRING(out);
    Py_ssize_t samples = in.len / 2;

    /* Fixed-point gain (Q16) so the common (quiet-enough) path is integer-only. */
    if (volume < 0.0) volume = 0.0;
    if (volume > 4.0) volume = 4.0;
    int32_t gain = (int32_t)(volume * 65536.0 + 0.5);

    Py_BEGIN_ALLOW_THREADS
    for (Py_ssize_t i = 0; i < samples; i++) {
        /* Read/write little-endian explicitly so this is correct on any host. */
        int16_t s = (int16_t)(src[2 * i] | (src[2 * i + 1] << 8));
        int64_t v = ((int64_t)s * gain) >> 16;
        if (v > LIMIT_KNEE || v < -LIMIT_KNEE) {
            double over = (fabs((double)v) - LIMIT_KNEE) / LIMIT_RANGE;
            double limited = LIMIT_KNEE + LIMIT_RANGE * tanh(over);
            v = (int64_t)(v > 0 ? limited : -limited);
        }
        dst[2 * i] = (uint8_t)(v & 0xFF);
        dst[2 * i + 1] = (uint8_t)((v >> 8) & 0xFF);
    }
    Py_END_ALLOW_THREADS

    PyBuffer_Release(&in);
    return out;
}

/*
 * Split UTF-8 text into lowercase tokens. ASCII letters and digits form
 * tokens; any other ASCII byte separates them. Non-ASCII bytes are kept as
 * token characters so titles in other scripts still compare sensibly.
 */
static int tokenize(const char *text, Py_ssize_t len, char tokens[][MAX_TOKEN_LEN]) {
    int count = 0, cur = 0;

    for (Py_ssize_t i = 0; i <= len && count < MAX_TOKENS; i++) {
        unsigned char c = i < len ? (unsigned char)text[i] : ' ';
        int is_word = (c >= 0x80) || (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');

        if (is_word) {
            if (cur < MAX_TOKEN_LEN - 1)
                tokens[count][cur++] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : (char)c;
        } else if (cur > 0) {
            tokens[count][cur] = '\0';
            count++;
            cur = 0;
        }
    }
    return count;
}

/* Dice coefficient over unique tokens: 2|A∩B| / (|A|+|B|), in [0, 1]. */
static PyObject *match_score(PyObject *self, PyObject *args) {
    const char *a, *b;
    Py_ssize_t alen, blen;

    if (!PyArg_ParseTuple(args, "s#s#", &a, &alen, &b, &blen))
        return NULL;

    static char ta[MAX_TOKENS][MAX_TOKEN_LEN];
    static char tb[MAX_TOKENS][MAX_TOKEN_LEN];
    int na = tokenize(a, alen, ta);
    int nb = tokenize(b, blen, tb);

    if (na == 0 || nb == 0)
        return PyFloat_FromDouble(0.0);

    /* Deduplicate A so a repeated word can't inflate the overlap. */
    int ua = 0;
    for (int i = 0; i < na; i++) {
        int dup = 0;
        for (int j = 0; j < ua; j++)
            if (strcmp(ta[i], ta[j]) == 0) { dup = 1; break; }
        if (!dup && ua != i) memcpy(ta[ua], ta[i], MAX_TOKEN_LEN);
        if (!dup) ua++;
    }

    int ub = 0;
    for (int i = 0; i < nb; i++) {
        int dup = 0;
        for (int j = 0; j < ub; j++)
            if (strcmp(tb[i], tb[j]) == 0) { dup = 1; break; }
        if (!dup && ub != i) memcpy(tb[ub], tb[i], MAX_TOKEN_LEN);
        if (!dup) ub++;
    }

    int shared = 0;
    for (int i = 0; i < ua; i++)
        for (int j = 0; j < ub; j++)
            if (strcmp(ta[i], tb[j]) == 0) { shared++; break; }

    return PyFloat_FromDouble(2.0 * shared / (double)(ua + ub));
}

static PyMethodDef FastAudioMethods[] = {
    {"scale_pcm", scale_pcm, METH_VARARGS,
     "scale_pcm(data: bytes, volume: float) -> bytes\n"
     "Scale 16-bit LE PCM by volume (0.0-4.0); peaks are soft-limited, never clipped."},
    {"match_score", match_score, METH_VARARGS,
     "match_score(a: str, b: str) -> float\n"
     "Case-insensitive token overlap (Dice coefficient) between two strings."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef fastaudio_module = {
    PyModuleDef_HEAD_INIT, "fastaudio", "C hot paths for the music bot.", -1, FastAudioMethods,
};

PyMODINIT_FUNC PyInit_fastaudio(void) {
    return PyModule_Create(&fastaudio_module);
}
