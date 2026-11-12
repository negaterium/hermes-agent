#!/usr/bin/env node
// Deployment composition only: the native compilers own product layout and receipts.
import { statSync } from 'node:fs'
import path from 'node:path'
import { parseArgs } from 'node:util'
import { generateIcons } from '../../scripts/generate-icons.mjs'
import { buildWeb } from '../../scripts/build/web.mjs'
import { buildTui } from '../../scripts/build/tui.mjs'
import { productCurrent } from '../../scripts/build/freshness.mjs'
import { isMain } from '../../scripts/build/frontend-common.mjs'

function prepareIcons({ source, out }) {
  if (generateIcons(['--source', source, '--out', out]) !== 0) {
    throw new Error('Native icon preparation failed')
  }
}

export async function buildFrontends({ source, products }, {
  icons = prepareIcons, web = buildWeb, tui = buildTui, current = productCurrent,
} = {}) {
  source = path.resolve(source)
  products = path.resolve(products)
  const prepared = path.join(products, 'icons')
  const outputs = { web: path.join(products, 'web'), tui: path.join(products, 'tui') }
  await icons({ source, out: prepared })
  await web({ source, icons: prepared, out: outputs.web })
  await tui({ source, out: outputs.tui })
  for (const [product, out, entry] of [
    ['web', outputs.web, 'index.html'],
    ['tui', path.join(outputs.tui, 'dist'), 'entry.js'],
  ]) {
    const asset = path.join(out, entry)
    const stat = statSync(asset, { throwIfNoEntry: false })
    if (!stat?.isFile() || stat.size === 0) throw new Error(`Missing frontend asset: ${asset}`)
    if (!current({ source, product, out })) throw new Error(`Frontend freshness receipt is not current: ${out}`)
  }
  return outputs
}

if (isMain(import.meta.url)) {
  const { values } = parseArgs({ options: { source: { type: 'string' }, products: { type: 'string' } } })
  if (!values.source || !values.products) throw new Error('--source and --products are required')
  console.log(JSON.stringify(await buildFrontends(values)))
}
