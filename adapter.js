const readline = require('readline');
const decrypt = require('./acw_sc_v2.js');

const rl = readline.createInterface({
  input: process.stdin, crlfDelay: Infinity,
});

rl.on('line', (line) => {
  try {
    decrypt(line.trim());
  } catch (err) {}
});

rl.on('close', () => {
  process.exit(0);
});
