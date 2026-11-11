import { describe, expect, it } from 'vitest'

import { apiErrorMessage, formatQuantity, needsSignIn, scanLinesSum, sourceLabel, stockLevel } from './receipt-utils'
import type { ShoppingConsumption } from '@/types'

const consumption = (overrides: Partial<ShoppingConsumption>): ShoppingConsumption => ({
  purchase_count: 3,
  avg_days_between: 14,
  days_per_unit: 14,
  units_per_month: 2.1,
  monthly_cost: '42.86',
  estimated_depletion: '2026-10-13',
  days_left: 7,
  ...overrides,
})

describe('stockLevel', () => {
  it('is unknown without a forecast', () => {
    expect(stockLevel(null)).toBe('unknown')
    expect(stockLevel(consumption({ days_left: null }))).toBe('unknown')
  })

  it('is out once the forecast date has passed', () => {
    expect(stockLevel(consumption({ days_left: 0 }))).toBe('out')
    expect(stockLevel(consumption({ days_left: -4 }))).toBe('out')
  })

  it('is low in the last fifth of the interval, never under three days', () => {
    expect(stockLevel(consumption({ avg_days_between: 30, days_left: 6 }))).toBe('low')
    expect(stockLevel(consumption({ avg_days_between: 30, days_left: 7 }))).toBe('ok')
    expect(stockLevel(consumption({ avg_days_between: 2, days_left: 3 }))).toBe('low')
  })
})

describe('helpers', () => {
  it('names the store chains', () => {
    expect(sourceLabel('lidl_plus')).toBe('Lidl')
    expect(sourceLabel('biedronka')).toBe('Biedronka')
    expect(sourceLabel('other')).toBe('other')
  })

  it('reads the error detail the API sends', () => {
    expect(apiErrorMessage({ response: { data: { detail: 'The store rejected the sign-in' } } })).toBe(
      'The store rejected the sign-in',
    )
    expect(apiErrorMessage({ response: { data: { detail: { message: 'x' } } } })).toBe('x')
    expect(apiErrorMessage(new Error('boom'))).toBeNull()
  })

  it('asks for a sign-in only when the store needs one', () => {
    expect(needsSignIn('pending')).toBe(true)
    expect(needsSignIn('reauth_required')).toBe(true)
    expect(needsSignIn('error')).toBe(false)
  })

  it('formats pieces and weights', () => {
    expect(formatQuantity('2.000', 'en')).toBe('2')
    expect(formatQuantity('0.598', 'en')).toBe('0.598')
  })
})

describe('scanLinesSum', () => {
  it('adds lines after their discounts, in whole cents', () => {
    const line = (total: string, discount = '0') => ({ name: 'x', quantity: '1', unit: null, unit_price: total, total, discount })
    expect(scanLinesSum([line('19.99', '3.00'), line('4.47')])).toBe(21.46)
    expect(scanLinesSum([line('0.10'), line('0.20')])).toBe(0.3)
    expect(scanLinesSum([line('')])).toBe(0)
  })
})
