/** Render the small, non-executable UI vocabulary without HTML injection sinks.
 * Values are escaped by the templates, then decoded only into text/attributes.
 * Scripts, URL attributes, event attributes and arbitrary CSS are never accepted.
 */
function sharedCardSafeReplace(target, markup) {
  const tags = new Set('div span header footer nav main section aside h1 h2 h3 p button label input select option table thead tbody tr th td strong b i em small br details summary'.split(' '));
  const attrs = new Set('id class role title type value disabled checked selected colspan for placeholder tabindex'.split(' '));
  const decode = value => value.replace(/&(amp|lt|gt|quot|#39|nbsp);/g, (_, name) => ({amp:'&',lt:'<',gt:'>',quot:'"','#39':"'",nbsp:'\u00a0'}[name]));
  const fragment = document.createDocumentFragment(), stack = [fragment];
  for (const token of String(markup).match(/<[^>]*>|[^<]+|</g) || []) {
    if (!token.startsWith('<')) { stack.at(-1).append(document.createTextNode(decode(token))); continue; }
    const match = token.match(/^<(\/?)([a-z][a-z0-9]*)([\s\S]*?)>$/i);
    if (!match || !tags.has(match[2].toLowerCase())) throw new Error('不支持的界面元素');
    const tag = match[2].toLowerCase();
    if (match[1]) {
      if (stack.length < 2 || stack.at(-1).localName !== tag) throw new Error('界面元素闭合不一致：'+tag);
      stack.pop(); continue;
    }
    const element = document.createElement(tag);
    let rest = match[3].trim().replace(/\/$/, '').trim();
    while (rest) {
      const attribute = rest.match(/^([a-z][a-z0-9-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>`=]+)))?\s*/i);
      if (!attribute) throw new Error('不支持的界面属性');
      const name = attribute[1].toLowerCase(), value = decode(attribute[2] ?? attribute[3] ?? attribute[4] ?? '');
      if (attrs.has(name) || /^(aria|data)-[a-z0-9-]+$/.test(name)) element.setAttribute(name, value);
      else if (name === 'style') {
        for (const declaration of value.split(';').filter(x=>x.trim())) {
          const css = declaration.match(/^\s*(padding|margin-top|margin-bottom|width|max-width|min-width|display|gap)\s*:\s*([a-z0-9.%\s-]+)\s*$/i);
          if (!css) throw new Error('不支持的界面样式');
          element.style.setProperty(css[1], css[2]);
        }
      } else throw new Error('不支持的界面属性：'+name);
      rest = rest.slice(attribute[0].length);
    }
    stack.at(-1).append(element);
    if (!['input','br'].includes(tag)) stack.push(element);
  }
  if (stack.length !== 1) throw new Error('界面元素未闭合');
  target.replaceChildren(fragment);
}
