'use strict';
const fs = require('fs');
const path = require('path');
const cp = require('child_process');
const root = cp.execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim();
const pkg = path.join(root, '@tobilu/qmd');
if (JSON.parse(fs.readFileSync(path.join(pkg, 'package.json'), 'utf8')).version !== '2.8.3') {
  throw new Error('QMD patch requires reviewed version 2.8.3');
}
const file = path.join(pkg, 'dist/llm.js');
const source = fs.readFileSync(file, 'utf8');
const target = /const loadLlama = async \(gpu, sourceBuildAllowed = canBuild, buildOverride\) => await withNativeStdoutRedirectedToStderr\(\(\) => getLlama\(\{\n[\s\S]*?^\s*skipDownload: !sourceBuildAllowed,\n\s*\}\)\);/m;
const replacement = `const loadLlama = async (gpu, sourceBuildAllowed = canBuild, buildOverride) => await withNativeStdoutRedirectedToStderr(() => getLlama({
                build: "never",
                logLevel: LlamaLogLevel.error,
                gpu: false,
                progressLogs: false,
                skipDownload: true,
            }));`;
if (source.includes(replacement)) {
  console.log(`QMD CPU prebuilt patch already present at ${file}.`);
} else {
  const matches = [...source.matchAll(new RegExp(target.source, 'gm'))];
  if (matches.length !== 1) throw new Error('QMD patch failed: expected exactly one reviewed loadLlama stanza');
  fs.writeFileSync(file, source.replace(target, replacement));
  console.log(`QMD llm.js patched for CPU prebuilt mode at ${file}.`);
}
