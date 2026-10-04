import { exportHtmlToPptx } from './node_modules/dom-to-pptx/dist/dom-to-pptx-node.mjs';
import fs from 'fs';
import path from 'path';

// 用法: node batch_export.mjs <htmlDir> <outDir>
// 将 htmlDir 下 slideNN.html 逐页转换为 outDir/slideNN.pptx（dom-to-pptx 单页）
const [,, htmlDirArg, outDirArg] = process.argv;
const htmlDir = htmlDirArg || 'D:/project/AI Agent/PPT Agent/workspace/html_source';
const outDir = outDirArg || 'D:/project/AI Agent/PPT Agent/output/dom2pptx_pages';
fs.mkdirSync(outDir, { recursive: true });

const files = fs.readdirSync(htmlDir)
  .filter(f => /^slide\d+\.html$/.test(f))
  .sort((a, b) => {
    const na = parseInt(a.match(/\d+/)[0], 10), nb = parseInt(b.match(/\d+/)[0], 10);
    return na - nb;
  });
console.log('pages:', files.length, files[0], '...', files[files.length - 1]);

let ok = 0, fail = 0;
for (const f of files) {
  const htmlPath = path.join(htmlDir, f);
  const outPath = path.join(outDir, f.replace('.html', '.pptx'));
  try {
    const buf = await exportHtmlToPptx(htmlPath, {
      selector: '.slide',
      injectBundle: false,
      browserWidth: 1280,
      browserHeight: 720,
      pptxOptions: { width: 10, height: 5.625, includePseudoElements: true },
    });
    fs.writeFileSync(outPath, buf);
    ok++;
    console.log('OK', f, buf.length, 'bytes');
  } catch (e) {
    fail++;
    console.log('FAIL', f, e.message);
  }
}
console.log(`batch done: ok=${ok} fail=${fail}`);
process.exit(fail > 0 ? 1 : 0);
