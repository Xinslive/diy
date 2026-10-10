/** 金额转中文大写。合计行要写「零元整」而不是 0.00，这是记账的老规矩。 */

const DIGITS = ['零', '壹', '贰', '叁', '肆', '伍', '陆', '柒', '捌', '玖']
const UNITS = ['', '拾', '佰', '仟']
const SECTIONS = ['', '万', '亿', '万亿']

/** 一段四位以内的数转中文：1234 -> 壹仟贰佰叁拾肆，1001 -> 壹仟零壹 */
function partToChinese(part) {
  let seg = ''
  let p = part
  let pos = 0
  let pendingZero = false
  while (p > 0) {
    const d = p % 10
    if (d === 0) {
      // 中间的零只留一个；段首的零直接丢掉（由外层决定要不要补）
      if (seg) pendingZero = true
    } else {
      seg = DIGITS[d] + UNITS[pos] + (pendingZero ? '零' : '') + seg
      pendingZero = false
    }
    p = Math.floor(p / 10)
    pos += 1
  }
  return seg
}

/** 整数部分转中文。10000 -> 壹万，100010001 -> 壹亿零壹万零壹 */
function intToChinese(n) {
  if (n === 0) return '零'
  let out = ''
  let section = 0
  // 低位那组不足 1000（万位上是 0）时，新接上的高位段前面要补一个零
  let needZero = false
  while (n > 0) {
    const part = n % 10000
    if (part > 0) {
      out = partToChinese(part) + SECTIONS[section] + (needZero ? '零' : '') + out
      needZero = part < 1000
    } else if (out) {
      out = '零' + out
      needZero = true
    }
    n = Math.floor(n / 10000)
    section += 1
  }
  return out
}

/** 1234.56 -> 壹仟贰佰叁拾肆元伍角陆分；0 -> 零元整 */
export function amountToChinese(value) {
  const n = Number(value || 0)
  if (!Number.isFinite(n)) return '零元整'
  if (n === 0) return '零元整'

  const neg = n < 0
  const abs = Math.abs(n)
  const [intPart, decPart] = abs.toFixed(2).split('.')
  const jiao = Number(decPart[0])
  const fen = Number(decPart[1])

  const intVal = Number(intPart)
  // 不足 1 元时不带「元」字：0.01 读作「壹分」而不是「零元壹分」
  let out = intVal > 0 ? intToChinese(intVal) + '元' : ''
  if (jiao === 0 && fen === 0) {
    out += intVal > 0 ? '整' : '零元整'
  } else {
    if (jiao > 0) out += DIGITS[jiao] + '角'
    else if (intVal > 0) out += '零'
    if (fen > 0) out += DIGITS[fen] + '分'
    else out += '整'
  }
  if (!out) out = '零元整'
  return (neg ? '负' : '') + out
}
