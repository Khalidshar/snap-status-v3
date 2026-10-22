import fs from 'node:fs/promises';
import os from 'node:os';
import {selftest} from '../snap_bot/madar/selftest.mjs';
import {recordSelftest} from '../snap_bot/madar/record-selftest.mjs';
const registry=JSON.parse(await fs.readFile(new URL('../snap_bot/madar/registry.json',import.meta.url),'utf8'));
const report=await selftest(os.tmpdir(),registry);
const cli=await recordSelftest(os.tmpdir());
console.log(JSON.stringify({passed:report.passed&&cli.passed,checks:[...report.checks,...cli.checks]}));
