import pptx
print('pptx', pptx.__version__)
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

prs = Presentation()
prs.slide_width = Inches(13.333); prs.slide_height = Inches(7.5)
s = prs.slides.add_slide(prs.slide_layouts[6])
cd = CategoryChartData()
cd.categories = ['08-01', '08-02', '08-03']
cd.add_series('价格', [100, 200, 150])
sh = s.shapes.add_chart(XL_CHART_TYPE.LINE, 0, 0, Inches(6), Inches(3), cd)
ch = sh.chart
cat = ch.category_axis
val = ch.value_axis
print('cat type', type(cat).__name__, 'val type', type(val).__name__)
print('catAx children:')
for child in cat._element:
    print('  ', child.tag.split('}')[-1])
print('valAx children:')
for child in val._element:
    print('  ', child.tag.split('}')[-1])
print('tick_labels attrs:', [m for m in dir(cat.tick_labels) if not m.startswith('_')])
print('has number_format', hasattr(cat.tick_labels, 'number_format'))
print('value axis has major_unit', hasattr(val, 'major_unit'), 'min', hasattr(val, 'minimum_scale'), 'max', hasattr(val, 'maximum_scale'))
from pptx.enum.chart import XL_MARKER_STYLE
print('marker none', XL_MARKER_STYLE.NONE)
print('marker none repr', XL_MARKER_STYLE.NONE)
from pptx.enum.chart import XL_TICK_LABEL_POSITION, XL_TICK_MARK
print('tick label pos', list(XL_TICK_LABEL_POSITION)[:5])
# check series has marker
ser = ch.series[0]
print('series has marker', hasattr(ser, 'marker'))
print('ser marker type', type(ser.marker).__name__)
print('ser line has width', hasattr(ser.format.line, 'width'))
