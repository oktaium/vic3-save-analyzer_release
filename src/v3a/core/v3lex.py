"""Low-level lexer for Paradox (Clausewitz/Jomini) binary save format used by Victoria 3.

Binary format: little-endian stream of u16 tokens.
  0x0001 '='       0x0003 '{'      0x0004 '}'
  value-type tokens are followed by a payload:
  0x000c i32 | 0x0014 u32 | 0x000d f32 | 0x0167 f64-ish (i64 fixed point /100000)
  0x029c u64 | 0x0317 i64 | 0x000e bool(u8)
  0x000f quoted str (u16 len + bytes) | 0x0017 unquoted str (u16 len + bytes)
  0x0243 rgb { r g b }
  Newer engines (Vic3 1.5+) add compact "fixed5" numbers and string-lookup tokens;
  their ids are listed in EXTRA below (verified empirically against these saves).
Any other u16 is a *field-name token* (an id into a name table that lives inside the
game executable; Paradox does not publish it). We keep it as 'k_XXXX' (hex).
"""
EQ, OPEN, CLOSE = 0x0001, 0x0003, 0x0004
I32, U32, F32, BOOL, QSTR, USTR = 0x000c, 0x0014, 0x000d, 0x000e, 0x000f, 0x0017
F64, U64, I64, RGB = 0x0167, 0x029c, 0x0317, 0x0243

# Compact number encodings (jomini "fixed5" family): value = signed int / 100000
# 0x0d48 zero, 0x0d49..0x0d4f positive with 1..7 payload bytes, 0x0d50..0x0d56 negative.
FIXED5_ZERO = 0x0d48
FIXED5_POS = {0x0d49 + i: i + 1 for i in range(7)}
FIXED5_NEG = {0x0d50 + i: i + 1 for i in range(7)}
# string lookup tokens (index into a string table) - u8 / u16 / u24 payloads
LOOKUP = {0x0d40: 1, 0x0d3e: 1, 0x0d41: 2, 0x0d3d: 2, 0x0d43: 3, 0x0d3f: 3}

