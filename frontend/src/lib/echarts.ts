import * as echarts from 'echarts/core'
import {
  BarChart,
  CandlestickChart,
  CustomChart,
  HeatmapChart,
  LineChart,
  PieChart,
} from 'echarts/charts'
import {
  AxisPointerComponent,
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  GraphicComponent,
  GridComponent,
  LegendPlainComponent,
  LegendScrollComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  TooltipComponent,
  VisualMapContinuousComponent,
} from 'echarts/components'
import { LabelLayout } from 'echarts/features'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  LineChart,
  BarChart,
  PieChart,
  CandlestickChart,
  HeatmapChart,
  CustomChart,
  GridComponent,
  TooltipComponent,
  AxisPointerComponent,
  LegendPlainComponent,
  LegendScrollComponent,
  DataZoomInsideComponent,
  DataZoomSliderComponent,
  VisualMapContinuousComponent,
  GraphicComponent,
  MarkPointComponent,
  MarkLineComponent,
  MarkAreaComponent,
  LabelLayout,
  CanvasRenderer,
])

export default echarts
export { init, getInstanceByDom, connect } from 'echarts/core'
export type { ECharts } from 'echarts/core'
export type { EChartsOption } from 'echarts'
