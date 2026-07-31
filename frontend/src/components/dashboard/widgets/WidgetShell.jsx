import React from 'react';

const WidgetShell = ({ children, title }) => {
  return React.createElement('div', { className: 'p-4 border rounded' }, title || children);
};

export default WidgetShell;