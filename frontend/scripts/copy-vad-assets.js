/**
 * Copy Silero VAD + onnxruntime-web runtime assets into public/vad/ so the
 * hands-free mic can load them same-origin (no CDN dependency — the kiosk
 * may run without general internet egress). Runs on postinstall.
 */
const fs = require('fs')
const path = require('path')

const outDir = path.join(__dirname, '..', 'public', 'vad')
fs.mkdirSync(outDir, { recursive: true })

const copies = []

const vadDist = path.join(__dirname, '..', 'node_modules', '@ricky0123', 'vad-web', 'dist')
for (const f of fs.readdirSync(vadDist)) {
  if (f.endsWith('.onnx') || f === 'vad.worklet.bundle.min.js') copies.push([vadDist, f])
}

const ortDist = path.join(__dirname, '..', 'node_modules', 'onnxruntime-web', 'dist')
for (const f of fs.readdirSync(ortDist)) {
  if (f.endsWith('.wasm') || f.endsWith('.mjs')) copies.push([ortDist, f])
}

for (const [dir, f] of copies) {
  fs.copyFileSync(path.join(dir, f), path.join(outDir, f))
}
console.log(`vad assets: copied ${copies.length} files to public/vad/`)
