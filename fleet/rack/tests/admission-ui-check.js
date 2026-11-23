// Use the rack's synthetic DOM to run the same render() as the browser.
const fs = require('fs');
const path = require('path');
const harness = fs.readFileSync(path.join(__dirname, 'reorder-ui-check.js'), 'utf8').split('(async()=>{', 1)[0];
eval(harness + '\nprocess.stdout.write(elements.app.innerHTML);');
