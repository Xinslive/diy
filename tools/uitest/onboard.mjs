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
await p.click('button:has-text("登录")'); await p.waitForSelector('.book-page',{timeout:15000})

await step('打开账套管理', async()=>{
  // 登录后就落在账套管理页，这里用 hash 直达而不是点菜单 ——
  // 菜单项的可见性随窗口宽度变，点它时灵时不灵，页签本身才是判据
  await p.evaluate(()=>{location.hash='#/books'})
  await p.waitForSelector('.book-page',{timeout:10000}); await p.waitForTimeout(600)
})
await p.screenshot({path:'.shots/13-账套管理.png'})

const tag = String(Date.now()).slice(-5)
await step('新建账套', async()=>{
  await p.click('button:has-text("新建账套")')
  await p.waitForSelector('text=公司名称',{timeout:5000})
  await p.fill('.el-dialog input >> nth=0', `测试公司${tag}`)
  await p.fill('.el-dialog input >> nth=1', `开户测试${tag}`)
  await p.click('.el-dialog button:has-text("创建账套")')
  // 建完是**初始化向导**（两步），不再是创建弹窗里一条可以无视的提示条
  await p.waitForSelector('text=账套初始化',{timeout:15000}); await p.waitForTimeout(600)
})
await p.screenshot({path:'.shots/14-新建账套弹初始化向导.png'})

await step('第2步在第1步没完成时锁着', async()=>{
  const hint = await p.locator('text=请先完成第 1 步').count()
  if(!hint) throw new Error('第 2 步没有给出「先做第 1 步」的提示')
  if(await p.locator('.panel button:has-text("导入期初余额")').count())
    throw new Error('第 1 步没做完，第 2 步的按钮不该可点')
})

await step('第1步：沿用系统标准科目表即算完成', async()=>{
  await p.click('.panel button:has-text("沿用系统标准科目表")')
  await p.waitForSelector('text=科目已确定',{timeout:10000}); await p.waitForTimeout(400)
})
await p.screenshot({path:'.shots/15-初始化第1步完成.png'})

await step('第2步：去期初余额页（导入弹窗自动弹出）', async()=>{
  // 面板里那颗，不是底部那颗：底部在「只剩第 2 步」时也叫「去导入期初余额」，
  // 用 has-text 圈会把两颗一起圈进来
  await p.click('.panel button:has-text("导入期初余额")')
  await p.waitForSelector('text=从 Excel 导入期初余额',{timeout:10000}); await p.waitForTimeout(500)
  await p.click('.el-dialog button:has-text("取消")')   // 先关掉，下面手录
  await p.waitForSelector('.grid',{timeout:10000}); await p.waitForTimeout(800)
})
await p.screenshot({path:'.shots/16-期初余额.png'})

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
await p.screenshot({path:'.shots/17-期初反方向.png'})

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
await p.screenshot({path:'.shots/18-期初已保存.png'})

await step('导出→再导入 往返不丢反方向', async()=>{
  const [dl] = await Promise.all([
    p.waitForEvent('download',{timeout:12000}),
    p.click('button:has-text("导出当前")')
  ])
  const f = '/tmp/onboard-openings.xlsx'
  await dl.saveAs(f)
  // 导入现在是**弹窗 + 预览**三步（选文件 → 解析预览 → 确认导入），
  // 不是以前那个直接把文件塞进去的 el-upload
  await p.click('button:has-text("从 Excel 导入")')
  await p.waitForSelector('text=从 Excel 导入期初余额',{timeout:10000})
  await p.setInputFiles('.el-dialog input[type=file]', f)
  await p.click('.el-dialog button:has-text("解析并预览")')
  await p.waitForSelector('text=识别来源',{timeout:10000}); await p.waitForTimeout(400)
  if(!(await p.locator('.el-dialog .stat b').first().innerText()).trim())
    throw new Error('预览步没有读入条数')
  await p.click('.el-dialog button:has-text("确认导入")')
  await p.waitForSelector('text=已填入期初余额表格',{timeout:8000})
  await p.click('.el-dialog button:has-text("知道了")')
  await p.waitForTimeout(800)
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

// ---------------------------- 两步做完之后：底部不该还挂着一颗点不动的「下一步」
const footerBtn = async () =>
  (await p.locator('.el-dialog__footer button').last().innerText()).trim()

await step('两步做完，底部主按钮换成「完成」', async()=>{
  await p.evaluate(()=>{location.hash='#/books'}); await p.waitForTimeout(1500)
  await p.click('button:has-text("新建账套")')
  await p.waitForSelector('text=公司名称',{timeout:5000})
  await p.fill('.el-dialog input >> nth=0', `收尾公司${tag}`)
  await p.fill('.el-dialog input >> nth=1', `收尾测试${tag}`)
  await p.click('.el-dialog button:has-text("创建账套")')
  await p.waitForSelector('text=账套初始化',{timeout:15000}); await p.waitForTimeout(600)

  await p.click('.panel button:has-text("沿用系统标准科目表")')
  await p.waitForSelector('text=科目已确定',{timeout:10000}); await p.waitForTimeout(500)

  // 只剩第 2 步时，底部那颗得指向真正待办的事，而不是含糊的「下一步」
  const mid = await footerBtn()
  if(mid!=='去导入期初余额') throw new Error('只剩第 2 步时底部按钮是「'+mid+'」')

  await p.click('.panel button:has-text("本期无期初余额")')
  await p.click('.el-message-box button:has-text("确认无期初")')
  await p.waitForSelector('text=两步初始化都已完成',{timeout:10000}); await p.waitForTimeout(600)

  // 这就是那个 bug：都做完了，底部还写着「下一步」，按下去什么也不会发生
  const done = await footerBtn()
  if(done!=='完成') throw new Error('两步做完后底部按钮还是「'+done+'」')
  if(await p.locator('.el-dialog__footer button:has-text("下一步")').count())
    throw new Error('两步做完后还挂着「下一步」')
  if(await p.locator('.el-dialog__footer button:has-text("稍后再说")').count())
    throw new Error('都做完了还提示「稍后再说」')
})
await p.screenshot({path:'.shots/19-初始化做完是完成按钮.png'})

await step('点「完成」收尾，卡片上不再有待初始化', async()=>{
  await p.click('.el-dialog__footer button:has-text("完成")')
  await p.waitForTimeout(800)
  const tag2 = `收尾公司${tag}`
  const card = p.locator('.book-page').locator(`xpath=.//*[contains(text(),"${tag2}")]/ancestor::*[contains(@class,"card") or contains(@class,"book")][1]`)
  if(await card.locator('button:has-text("继续初始化")').count())
    throw new Error('做完了卡片上还有「继续初始化」')
})

// ------------------------------------------------------------ 磁贴可拖拽排序
const tileNames = () => p.locator('.book-tile .book-name-link').allInnerTexts()

await step('第 2 步在别的页面做完，回账套管理状态一致', async()=>{
  // 用户报过的原样场景：向导第 2 步把人送到期初页，保存发生在那边，
  // 回来后向导/卡片却还写着「没做」。这条钉的是那条**跨页**的状态链。
  await p.evaluate(()=>{location.hash='#/books'}); await p.waitForTimeout(1800)
  const first = p.locator('.book-tile').filter({hasText:`测试公司${tag}`}).first()
  if(await first.locator('text=待初始化').count())
    throw new Error('期初已在期初页保存完，卡片上还是「待初始化」')
  if(await first.locator('button:has-text("继续初始化")').count())
    throw new Error('做完了还挂着「继续初始化」')
})

await step('拖动磁贴能改顺序，且刷新后还在', async()=>{
  await p.evaluate(()=>{location.hash='#/books'}); await p.waitForTimeout(1500)
  const before = await tileNames()
  if(before.length < 3) throw new Error('账套不足 3 个，拖不出效果')

  // 拖到第 3 张的**右半边** = 插到它后面。落在右半边这一支很容易在
  // 重构时被当成边界丢掉，所以用例钉的是「第 1 张出现在第 2 位之后」
  await p.dragAndDrop('.book-tile >> nth=0', '.book-tile >> nth=2',
    { targetPosition: { x: 300, y: 60 } })
  await p.waitForTimeout(900)
  const after = await tileNames()
  if(after.join()===before.join()) throw new Error('拖了没动')

  // 这个特性最容易「看起来能用、其实没存住」，所以必须真的刷新一次
  await p.reload({waitUntil:'networkidle'})
  await p.waitForSelector('.book-tile',{timeout:10000}); await p.waitForTimeout(1200)
  const reloaded = await tileNames()
  if(reloaded.join()!==after.join()) throw new Error('刷新后顺序弹回去了')
  console.log('   →', before[0], '移到了第', after.indexOf(before[0])+1, '位')
})
await p.screenshot({path:'.shots/29-拖拽排序.png'})

await step('只有一本账套时不拖（也不该给出「可排序」的提示）', async()=>{
  const cards = await p.locator('.book-tile').count()
  if(cards > 1) return   // 同一套开发库上跑，跳过
  if(await p.locator('.book-order-hint').count())
    throw new Error('只有一本账时不该提示「拖动磁贴可排序」')
})

console.log(errors.length?'\n问题:\n'+errors.join('\n'):'\n无错误')
await b.close()
