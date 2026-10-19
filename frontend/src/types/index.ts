// 类型定义

export interface Location {
  longitude: number
  latitude: number
}

export interface Attraction {
  name: string
  address: string
  location: Location
  visit_duration: number
  description: string
  category?: string
  rating?: number
  image_url?: string
  ticket_price?: number
}

export interface Meal {
  type: 'breakfast' | 'lunch' | 'dinner' | 'snack'
  name: string
  address?: string
  location?: Location
  description?: string
  estimated_cost?: number
}

export interface Hotel {
  name: string
  address: string
  location?: Location
  price_range: string
  rating: string
  distance: string
  type: string
  estimated_cost?: number
}

export interface Budget {
  total_attractions: number
  total_hotels: number
  total_meals: number
  total_transportation: number
  total: number
}

export interface DayPlan {
  date: string
  day_index: number
  description: string
  transportation: string
  accommodation: string
  hotel?: Hotel
  attractions: Attraction[]
  meals: Meal[]
}

export interface WeatherInfo {
  date: string
  day_weather: string
  night_weather: string
  day_temp: number
  night_temp: number
  wind_direction: string
  wind_power: string
}

export interface TripPlan {
  city: string
  start_date: string
  end_date: string
  days: DayPlan[]
  weather_info: WeatherInfo[]
  overall_suggestions: string
  budget?: Budget
}

export interface TripFormData {
  city: string
  start_date: string
  end_date: string
  travel_days: number
  transportation: string
  accommodation: string
  preferences: string[]
  free_text_input: string
}

export interface TripPlanResponse {
  success: boolean
  message: string
  data?: TripPlan
  status?: 'succeeded' | 'degraded' | 'failed'
  warnings?: string[]
}

export interface User {
  id: number
  username: string
  is_admin: boolean
  created_at: string
}

export interface AuthTokenResponse {
  access_token: string
  token_type: 'bearer'
  expires_in: number
  user: User
}

export type TaskStatus = 'queued' | 'running' | 'succeeded' | 'degraded' | 'failed'
export type ToolCallStatus = 'running' | 'succeeded' | 'empty' | 'failed'

export interface TaskError {
  code: string
  message: string
}

export interface ToolCall {
  id: number
  agent_name: string
  tool_name: string
  status: ToolCallStatus
  arguments: Record<string, unknown>
  result_summary?: Record<string, unknown> | null
  error?: TaskError | null
  started_at: string
  completed_at?: string | null
  duration_ms?: number | null
}

export interface TaskListItem {
  task_id: string
  status: TaskStatus
  stage: string
  city: string
  start_date: string
  end_date: string
  created_at: string
  updated_at: string
  completed_at?: string | null
  total_duration_ms?: number | null
}

export interface TaskListResponse {
  items: TaskListItem[]
  total: number
  page: number
  page_size: number
}

export interface TaskSubmitResponse {
  task_id: string
  status: TaskStatus
}

export interface TaskDetail {
  task_id: string
  status: TaskStatus
  stage: string
  request: TripFormData
  plan?: TripPlan | null
  warnings: string[]
  error?: TaskError | null
  tool_calls: ToolCall[]
  created_at: string
  started_at?: string | null
  completed_at?: string | null
  updated_at: string
  total_duration_ms?: number | null
}

export interface AdminTaskListItem extends TaskListItem {
  user_id: number
  username: string
}

export interface AdminTaskListResponse {
  items: AdminTaskListItem[]
  total: number
  page: number
  page_size: number
}

export interface AdminTaskDetail extends TaskDetail {
  user_id: number
  username: string
}
