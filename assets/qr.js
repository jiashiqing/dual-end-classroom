/* ==========================================================================
   qr.js —— 极简 QR 码编码器（零依赖，纯前端 canvas 用）
   --------------------------------------------------------------------------
   教室里的机器常年不联外网，所以二维码不能调在线接口、也不能引 CDN 库。
   这里按 ISO/IEC 18004 的标准流程自己算：8bit byte 模式 → RS 纠错 → 掩码择优，
   最后只给出一张 0/1 方阵，画多大多小由调用方决定。

   用法：
       const r = QRCode.encode('http://192.168.3.87:8099/s', 'M');
       r.size      // 边长（模块数，不含静区）
       r.modules   // r.size × r.size 的二维数组，1 = 黑
   已验证：生成 25/29 模块的码，OpenCV QRCodeDetector 逐字解回原文。
   ========================================================================== */
(function (root) {
'use strict';

/* ---------------- GF(256) 伽罗华域 ---------------- */
var EXP = new Uint8Array(512);
var LOG = new Uint8Array(256);
(function () {
  var x = 1;
  for (var i = 0; i < 255; i++) {
    EXP[i] = x;
    LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d;
  }
  for (var j = 255; j < 512; j++) EXP[j] = EXP[j - 255];
  LOG[0] = 0;
})();

function gmul(a, b) {
  if (a === 0 || b === 0) return 0;
  return EXP[LOG[a] + LOG[b]];
}

/* 生成多项式（最高次在前） */
function rsGeneratorPoly(degree) {
  var poly = [1];
  for (var i = 0; i < degree; i++) {
    var next = new Array(poly.length + 1);
    for (var k = 0; k < next.length; k++) next[k] = 0;
    for (var j = 0; j < poly.length; j++) {
      var c = poly[j];
      if (c === 0) continue;
      next[j] ^= c;                       // × x
      next[j + 1] ^= gmul(c, EXP[i]);     // × α^i
    }
    poly = next;
  }
  return poly;
}

/* 计算 RS 纠错码字 */
function rsEncode(data, ecLen) {
  var gen = rsGeneratorPoly(ecLen);
  var buf = new Array(data.length + ecLen);
  for (var i = 0; i < data.length; i++) buf[i] = data[i];
  for (var j = data.length; j < buf.length; j++) buf[j] = 0;
  for (var p = 0; p < data.length; p++) {
    var coef = buf[p];
    if (coef === 0) continue;
    for (var g = 0; g < gen.length; g++) buf[p + g] ^= gmul(gen[g], coef);
  }
  return buf.slice(data.length);
}

/* ---------------- 版本 / 纠错分块表 ---------------- */
/* [每块纠错码字数, 组1块数, 组1数据码字, 组2块数, 组2数据码字] */
var EC_TABLE = {
  1:  { L: [7, 1, 19, 0, 0], M: [10, 1, 16, 0, 0], Q: [13, 1, 13, 0, 0], H: [17, 1, 9, 0, 0] },
  2:  { L: [10, 1, 34, 0, 0], M: [16, 1, 28, 0, 0], Q: [22, 1, 22, 0, 0], H: [28, 1, 16, 0, 0] },
  3:  { L: [15, 1, 55, 0, 0], M: [26, 1, 44, 0, 0], Q: [18, 2, 17, 0, 0], H: [22, 2, 13, 0, 0] },
  4:  { L: [20, 1, 80, 0, 0], M: [18, 2, 32, 0, 0], Q: [26, 2, 24, 0, 0], H: [16, 4, 9, 0, 0] },
  5:  { L: [26, 1, 108, 0, 0], M: [24, 2, 43, 0, 0], Q: [18, 2, 15, 2, 16], H: [22, 2, 11, 2, 12] },
  6:  { L: [18, 2, 68, 0, 0], M: [16, 4, 27, 0, 0], Q: [24, 4, 19, 0, 0], H: [28, 4, 15, 0, 0] },
  7:  { L: [20, 2, 78, 0, 0], M: [18, 4, 31, 0, 0], Q: [18, 2, 14, 4, 15], H: [26, 4, 13, 1, 14] },
  8:  { L: [24, 2, 97, 0, 0], M: [22, 2, 38, 2, 39], Q: [22, 4, 18, 2, 19], H: [26, 4, 14, 2, 15] },
  9:  { L: [30, 2, 116, 0, 0], M: [22, 3, 36, 2, 37], Q: [20, 4, 16, 4, 17], H: [24, 4, 12, 4, 13] },
  10: { L: [18, 2, 68, 2, 69], M: [26, 4, 43, 1, 44], Q: [24, 6, 19, 2, 20], H: [28, 6, 15, 2, 16] }
};

var ECC_BITS = { L: 1, M: 0, Q: 3, H: 2 };
var MAX_VERSION = 10;

function ecInfo(ver, level) { return EC_TABLE[ver][level]; }
function dataCapacityBytes(ver, level) {
  var t = ecInfo(ver, level);
  return t[1] * t[2] + t[3] * t[4];
}

/* ---------------- 文本 → 字节 ---------------- */
function utf8Bytes(str) {
  var out = [], s = String(str);
  for (var i = 0; i < s.length; i++) {
    var c = s.charCodeAt(i);
    if (c < 0x80) out.push(c);
    else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 63));
    else if (c >= 0xd800 && c < 0xdc00 && i + 1 < s.length) {
      var c2 = s.charCodeAt(++i);
      var cp = 0x10000 + ((c - 0xd800) << 10) + (c2 - 0xdc00);
      out.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 63), 0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63));
    } else out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
  }
  return out;
}

/* ---------------- 位缓冲 ---------------- */
function BitBuffer() { this.bits = []; }
BitBuffer.prototype.put = function (val, len) {
  for (var i = len - 1; i >= 0; i--) this.bits.push((val >>> i) & 1);
};
BitBuffer.prototype.length = function () { return this.bits.length; };

/* ---------------- 数据编码 + 纠错 + 交织 ---------------- */
function buildCodewords(bytes, ver, level) {
  var buf = new BitBuffer();
  buf.put(0x4, 4);                                   // byte 模式指示符
  var ccBits = ver <= 9 ? 8 : 16;
  buf.put(bytes.length, ccBits);
  for (var i = 0; i < bytes.length; i++) buf.put(bytes[i], 8);

  var capacityBits = dataCapacityBytes(ver, level) * 8;
  // 终止符（最多 4 位）
  var term = Math.min(4, capacityBits - buf.length());
  buf.put(0, term);
  // 补齐到字节边界
  while (buf.length() % 8 !== 0) buf.bits.push(0);
  // 填充字节 0xEC / 0x11
  var padBytes = [0xec, 0x11], pi = 0;
  while (buf.length() < capacityBits) { buf.put(padBytes[pi++ % 2], 8); }

  var dataCw = [];
  for (var b = 0; b < buf.length(); b += 8) {
    var v = 0;
    for (var k = 0; k < 8; k++) v = (v << 1) | buf.bits[b + k];
    dataCw.push(v);
  }

  var t = ecInfo(ver, level);
  var ecLen = t[0], g1n = t[1], g1d = t[2], g2n = t[3], g2d = t[4];
  var blocks = [], pos = 0;
  for (var a = 0; a < g1n; a++) { blocks.push(dataCw.slice(pos, pos + g1d)); pos += g1d; }
  for (var c = 0; c < g2n; c++) { blocks.push(dataCw.slice(pos, pos + g2d)); pos += g2d; }

  var ecBlocks = blocks.map(function (blk) { return rsEncode(blk, ecLen); });
  var maxData = Math.max.apply(null, blocks.map(function (b2) { return b2.length; }));

  var out = [];
  for (var i2 = 0; i2 < maxData; i2++) {
    for (var bi = 0; bi < blocks.length; bi++) {
      if (i2 < blocks[bi].length) out.push(blocks[bi][i2]);
    }
  }
  for (var e = 0; e < ecLen; e++) {
    for (var bj = 0; bj < ecBlocks.length; bj++) out.push(ecBlocks[bj][e]);
  }
  return out;
}

/* ---------------- 矩阵构建 ---------------- */
function Matrix(ver) {
  this.version = ver;
  this.size = ver * 4 + 17;
  this.mat = [];
  this.func = [];
  for (var r = 0; r < this.size; r++) {
    this.mat.push(new Array(this.size).fill(0));
    this.func.push(new Array(this.size).fill(false));
  }
}
Matrix.prototype.setFunc = function (row, col, val) {
  if (row < 0 || col < 0 || row >= this.size || col >= this.size) return;
  this.mat[row][col] = val ? 1 : 0;
  this.func[row][col] = true;
};
Matrix.prototype.drawFinder = function (cx, cy) {
  for (var dy = -4; dy <= 4; dy++) {
    for (var dx = -4; dx <= 4; dx++) {
      var dist = Math.max(Math.abs(dx), Math.abs(dy));
      this.setFunc(cy + dy, cx + dx, dist !== 2 && dist !== 4);
    }
  }
};
Matrix.prototype.alignPositions = function () {
  var ver = this.version;
  if (ver === 1) return [];
  var num = Math.floor(ver / 7) + 2;
  var step = ver === 32 ? 26 : Math.floor((ver * 4 + 4) / (num * 2 - 2)) * 2;
  var res = new Array(num);
  res[0] = 6;
  var pos = ver * 4 + 10;
  for (var i = num - 1; i >= 1; i--) { res[i] = pos; pos -= step; }
  return res;
};
Matrix.prototype.drawFunctionPatterns = function () {
  var size = this.size, i;
  // 定位（时序）图案
  for (i = 0; i < size; i++) {
    this.setFunc(6, i, i % 2 === 0);
    this.setFunc(i, 6, i % 2 === 0);
  }
  // 三个寻像图形
  this.drawFinder(3, 3);
  this.drawFinder(size - 4, 3);
  this.drawFinder(3, size - 4);
  // 校正图形
  var ap = this.alignPositions();
  for (var a = 0; a < ap.length; a++) {
    for (var b = 0; b < ap.length; b++) {
      var isFinderCorner =
        (a === 0 && b === 0) ||
        (a === 0 && b === ap.length - 1) ||
        (a === ap.length - 1 && b === 0);
      if (isFinderCorner) continue;
      for (var dy = -2; dy <= 2; dy++) {
        for (var dx = -2; dx <= 2; dx++) {
          this.setFunc(ap[b] + dy, ap[a] + dx, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
        }
      }
    }
  }
  this.drawFormatBits(0);   // 占位，稍后用真实掩码重画
  this.drawVersionBits();
};
Matrix.prototype.drawFormatBits = function (mask) {
  var size = this.size;
  var data = (ECC_BITS[this._level] << 3) | mask;
  var rem = data << 10;
  for (var i = 0; i <= 4; i++) {          // 从最高位开始做多项式长除
    if (rem & (1 << (14 - i))) rem ^= 0x537 << (4 - i);
  }
  var bits = ((data << 10) | (rem & 0x3ff)) ^ 0x5412;
  var getBit = function (v, i2) { return (v >>> i2) & 1; };
  var i3;
  for (i3 = 0; i3 <= 5; i3++) this.setFunc(i3, 8, getBit(bits, i3));
  this.setFunc(7, 8, getBit(bits, 6));
  this.setFunc(8, 8, getBit(bits, 7));
  this.setFunc(8, 7, getBit(bits, 8));
  for (i3 = 9; i3 < 15; i3++) this.setFunc(8, 14 - i3, getBit(bits, i3));
  for (i3 = 0; i3 < 8; i3++) this.setFunc(8, size - 1 - i3, getBit(bits, i3));
  for (i3 = 8; i3 < 15; i3++) this.setFunc(size - 15 + i3, 8, getBit(bits, i3));
  this.setFunc(size - 8, 8, 1);   // 固定暗模块
};
Matrix.prototype.drawVersionBits = function () {
  var ver = this.version;
  if (ver < 7) return;
  var rem = ver;
  for (var i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
  var bits = (ver << 12) | (rem & 0xfff);
  for (var k = 0; k < 18; k++) {
    var bit = (bits >>> k) & 1;
    var a = this.size - 11 + (k % 3);
    var b = Math.floor(k / 3);
    this.setFunc(b, a, bit);
    this.setFunc(a, b, bit);
  }
};
Matrix.prototype.placeData = function (codewords) {
  var size = this.size, i = 0;
  var total = codewords.length * 8;
  for (var right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (var vert = 0; vert < size; vert++) {
      for (var j = 0; j < 2; j++) {
        var x = right - j;
        var upward = ((right + 1) & 2) === 0;
        var y = upward ? size - 1 - vert : vert;
        if (!this.func[y][x] && i < total) {
          var bit = (codewords[i >>> 3] >>> (7 - (i & 7))) & 1;
          this.mat[y][x] = bit;
          i++;
        }
      }
    }
  }
};
Matrix.prototype.applyMask = function (mask) {
  for (var y = 0; y < this.size; y++) {
    for (var x = 0; x < this.size; x++) {
      if (this.func[y][x]) continue;
      var inv;
      switch (mask) {
        case 0: inv = (x + y) % 2 === 0; break;
        case 1: inv = y % 2 === 0; break;
        case 2: inv = x % 3 === 0; break;
        case 3: inv = (x + y) % 3 === 0; break;
        case 4: inv = (Math.floor(y / 2) + Math.floor(x / 3)) % 2 === 0; break;
        case 5: inv = ((x * y) % 2) + ((x * y) % 3) === 0; break;
        case 6: inv = (((x * y) % 2) + ((x * y) % 3)) % 2 === 0; break;
        default: inv = (((x + y) % 2) + ((x * y) % 3)) % 2 === 0; break;
      }
      if (inv) this.mat[y][x] ^= 1;
    }
  }
};
Matrix.prototype.penalty = function () {
  var size = this.size, m = this.mat, score = 0, x, y, run, i;
  var PAT_A = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0];
  var PAT_B = [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1];
  // 规则1：同色连续 5 个以上
  for (y = 0; y < size; y++) {
    run = 1;
    for (x = 1; x < size; x++) {
      if (m[y][x] === m[y][x - 1]) run++;
      else { if (run >= 5) score += 3 + (run - 5); run = 1; }
    }
    if (run >= 5) score += 3 + (run - 5);
  }
  for (x = 0; x < size; x++) {
    run = 1;
    for (y = 1; y < size; y++) {
      if (m[y][x] === m[y - 1][x]) run++;
      else { if (run >= 5) score += 3 + (run - 5); run = 1; }
    }
    if (run >= 5) score += 3 + (run - 5);
  }
  // 规则2：2x2 同色块
  for (y = 0; y < size - 1; y++) {
    for (x = 0; x < size - 1; x++) {
      var v = m[y][x];
      if (v === m[y][x + 1] && v === m[y + 1][x] && v === m[y + 1][x + 1]) score += 3;
    }
  }
  // 规则3：出现类似寻像图形
  function rowMatch(get, idx) {
    var hits = 0;
    for (var s = 0; s + 11 <= size; s++) {
      var okA = true, okB = true;
      for (var k = 0; k < 11; k++) {
        var val = get(idx, s + k);
        if (val !== PAT_A[k]) okA = false;
        if (val !== PAT_B[k]) okB = false;
      }
      if (okA) hits++;
      if (okB) hits++;
    }
    return hits;
  }
  for (y = 0; y < size; y++) score += 40 * rowMatch(function (r, c) { return m[r][c]; }, y);
  for (x = 0; x < size; x++) score += 40 * rowMatch(function (r, c) { return m[c][r]; }, x);
  // 规则4：黑白比例
  var dark = 0;
  for (y = 0; y < size; y++) for (x = 0; x < size; x++) if (m[y][x]) dark++;
  var ratio = dark * 100 / (size * size);
  score += Math.floor(Math.abs(ratio - 50) / 5) * 10;
  return score;
};

/* ---------------- 主入口 ---------------- */
function encode(text, eccLevel) {
  var level = ECC_BITS[eccLevel] === undefined ? 'M' : String(eccLevel).toUpperCase();
  var bytes = utf8Bytes(text);

  var ver = -1;
  for (var v = 1; v <= MAX_VERSION; v++) {
    var ccBits = v <= 9 ? 8 : 16;
    if (4 + ccBits + bytes.length * 8 <= dataCapacityBytes(v, level) * 8) { ver = v; break; }
  }
  if (ver < 0) throw new Error('内容过长，超出二维码版本 1~10 的容量');

  var codewords = buildCodewords(bytes, ver, level);
  var best = null;
  for (var mask = 0; mask < 8; mask++) {
    var mt = new Matrix(ver);
    mt._level = level;
    mt.drawFunctionPatterns();
    mt.placeData(codewords);
    mt.applyMask(mask);
    mt.drawFormatBits(mask);
    var p = mt.penalty();
    if (!best || p < best.p) best = { p: p, mt: mt, mask: mask };
  }
  return {
    version: ver,
    ecc: level,
    mask: best.mask,
    size: best.mt.size,
    modules: best.mt.mat
  };
}

root.QRCode = { encode: encode };
})(typeof window !== 'undefined' ? window : this);
