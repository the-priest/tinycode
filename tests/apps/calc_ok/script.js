const display = document.getElementById('display');
let currentOperand = '0';
let previousOperand = '';
let operation = null;

function updateDisplay() {
  display.value = currentOperand;
}
function appendNumber(n) {
  if (n === '.' && currentOperand.includes('.')) return;
  currentOperand = currentOperand === '0' && n !== '.' ? n : currentOperand + n;
  updateDisplay();
}
function appendOperator(op) {
  if (operation !== null) calculate();
  operation = op;
  previousOperand = currentOperand;
  currentOperand = '0';
}
function calculate() {
  if (operation === null) return;
  const prev = parseFloat(previousOperand);
  const curr = parseFloat(currentOperand);
  let result;
  switch (operation) {
    case '+': result = prev + curr; break;
    case '-': result = prev - curr; break;
    case '*': result = prev * curr; break;
    case '/': result = curr === 0 ? 'Error' : prev / curr; break;
    case '%': result = prev % curr; break;
  }
  currentOperand = String(result);
  operation = null;
  updateDisplay();
}
function clearDisplay() { currentOperand = '0'; previousOperand = ''; operation = null; updateDisplay(); }
function deleteLast() { currentOperand = currentOperand.slice(0, -1) || '0'; updateDisplay(); }
document.addEventListener('keydown', (e) => {
  if (/\d/.test(e.key)) appendNumber(e.key);
  else if (e.key === 'Enter') calculate();
  else if (e.key === 'Escape') clearDisplay();
});
updateDisplay();
