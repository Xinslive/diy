import { chromium } from 'playwright'
const B='http://127.0.0.1:5173'
const errors=[]; const step=(n,f)=>f().then(()=>console.log('✓',n)).catch(e=>{console.log('✗',n,'—',e.message.split('\n')[0]);errors.push(n)})
const b=await chromium.launch()
const ctx=await b.newContext({acceptDownloads:true, viewport:{width:1560,height:1000}, deviceScaleFactor:2})
const p=await ctx.newPage()
p.on('console',m=>{if(m.type()==='error')errors.push('console: '+m.text())})
p.on('pageerror',e=>errors.push('pageerror: '+e.message))

await p.goto(B,{waitUntil:'networkidle'})
await p.fill('input[type="text"]','admin'); await p.fill('input[type="password"]','123456')
await p.click('button:has-text("登录")'); await p.waitForSelector('.el-aside',{timeout:15000})

await step('打开账套管理', async()=>{
  await p.click('.el-menu-item:has-text("账套管理")')
  await p.waitForSelector('text=账套名称',{timeout:10000}); await p.waitForTimeout(600)
})
await p.screenshot({path:'.shots/13-账套管理.png'})

const tag = String(Date.now()).slice(-5)
await step('新建账套', async()=>{
  await p.click('button:has-text("新建账套")')
  await p.waitForSelector('text=公司名称',{timeout:5000})
  await p.fill('.el-dialog input >> nth=0', `测试公司${tag}`)
  await p.fill('.el-dialog input >> nth=1', `开户测试${tag}`)
  await p.click('.el-dialog button:has-text("创建账套")')
  await p.waitForSelector('text=期初余额',{timeout:15000})
})
await p.screenshot({path:'.shots/14-新建账套.png'})

await step('跳到期初余额页', async()=>{
  await p.click('.el-dialog button:has-text("去录期初余额")')
  await p.waitForSelector('.grid',{timeout:10000}); await p.waitForTimeout(800)
})
await p.screenshot({path:'.shots/15-期初余额.png'})

await step('期初页可编辑', async()=>{
  const t = await p.textContent('.check')
  if(!t.includes('借贷平衡')) throw new Error('试算平衡条没出现: '+t)
})

await step('下载期初模板(鉴权)', async()=>{
  const codes=[]; ctx.on('response',r=>{if(/openings\.xlsx/.test(r.url()))codes.push(r.status())})
  const [dl]=await Promise.all([
    p.waitForEvent('download',{timeout:12000}),
    p.click('button:has-text("下载空白模板")')
  ])
  const st=codes[0]; if(st!==200) throw new Error('HTTP '+st)
  console.log('   →', dl.suggestedFilename(), st)
})

// ------------------------------------------------------------ 期初余额录入
const rowOf = (code) => p.locator('.grid tbody tr').filter({ has: p.locator(`td.code:text-is("${code}")`) })
const checkText = async () => (await p.textContent('.check')).replace(/\s+/g, ' ')

await step('录入期初三行', async()=>{
  await rowOf('100201').locator('input').fill('100000')
  await rowOf('1122').locator('input').fill('30000')
  await rowOf('4001').locator('input').fill('70000')
  await p.waitForTimeout(400)
  const t = await checkText()
  if(!t.includes('借方合计 130,000.00') || !t.includes('共 3 个科目有余额'))
    throw new Error('录入后试算不对: '+t)
})

await step('应收账款翻成贷方（反方向余额）后借贷平衡', async()=>{
  await rowOf('1122').locator('td.dir .flip').click()
  await p.waitForTimeout(300)
  const t = await checkText()
  if(!t.includes('借方合计 100,000.00') || !t.includes('贷方合计 100,000.00'))
    throw new Error('翻转后合计不对: '+t)
  if(!t.includes('借贷平衡')) throw new Error('应该显示平衡: '+t)
  const rev = await rowOf('1122').locator('td.dir .rev').count()
  if(rev !== 1) throw new Error('没有标出「反」方向')
})
await p.screenshot({path:'.shots/16-期初反方向.png'})

await step('不平的时候不能保存', async()=>{
  await rowOf('4001').locator('input').fill('60000')
  await p.waitForTimeout(300)
  const t = await checkText()
  if(!t.includes('借贷不平')) throw new Error('应该提示不平: '+t)
  const disabled = await p.locator('button:has-text("保存期初余额")').isDisabled()
  if(!disabled) throw new Error('不平时保存按钮应禁用')
  await rowOf('4001').locator('input').fill('70000')
  await p.waitForTimeout(300)
})

await step('保存期初并回读', async()=>{
  await p.click('button:has-text("保存期初余额")')
  await p.waitForSelector('.el-message--success',{timeout:10000})
  await p.reload({waitUntil:'networkidle'})
  await p.waitForSelector('.grid',{timeout:10000}); await p.waitForTimeout(700)
  const ar = await rowOf('1122').locator('input').inputValue()
  const side = (await rowOf('1122').locator('td.dir .flip').innerText()).trim()
  if(side!=='贷') throw new Error('回读后方向变成 '+side+'，反方向余额被洗掉了')
  if(Number(ar)!==30000) throw new Error('回读金额 '+ar)
  if(!(await rowOf('1122').locator('td.dir .rev').count())) throw new Error('回读后丢了「反」标记')
})
await p.screenshot({path:'.shots/17-期初已保存.png'})

await step('导出→再导入 往返不丢反方向', async()=>{
  const [dl] = await Promise.all([
    p.waitForEvent('download',{timeout:12000}),
    p.click('button:has-text("导出当前")')
  ])
  const f = '/tmp/onboard-openings.xlsx'
  await dl.saveAs(f)
  await p.setInputFiles('input[type=file]', f)
  await p.waitForTimeout(1500)
  const msg = await p.locator('.el-message').last().innerText()
  if(msg.includes('不平') || msg.includes('错误')) throw new Error('导入解析报错: '+msg)
  const side = (await rowOf('1122').locator('td.dir .flip').innerText()).trim()
  if(side!=='贷') throw new Error('往返后方向变成 '+side)
})

await step('导出 Excel 不再 401（余额表）', async()=>{
  const codes=[]; const h=r=>{if(/\.xlsx/.test(r.url()))codes.push(r.status()+' '+r.url())}
  ctx.on('response',h)
  await p.evaluate(()=>{location.hash='#/balance'}); await p.waitForTimeout(1500)
  const [dl]=await Promise.all([
    p.waitForEvent('download',{timeout:12000}),
    p.click('button:has-text("导出 Excel")')
  ])
  ctx.off('response',h)
  if(codes[0] && !codes[0].startsWith('200')) throw new Error(codes[0])
  console.log('   →', dl.suggestedFilename(), codes[0])
})

console.log(errors.length?'\n问题:\n'+errors.join('\n'):'\n无错误')
await b.close()
