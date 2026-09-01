import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import ActivityRail from '@/components/chat/ActivityRail';

describe('ActivityRail selected skill validation', () => {
  it('renders selected skill validation details without requiring factor.score', () => {
    render(
      <ActivityRail
        execution={{
          current_state: 'done',
          confidence_score: 0.55,
          observations: [],
          confidence_factors: {
            selected_skill_validation: {
              skill_name: 'weekly-sales-report',
              overall_score: 0.45,
              is_ok: false,
              missing_elements: ['kpis', 'recommendations'],
              issues: ['Missing selected-skill requirements: kpis, recommendations'],
            },
            verification: { score: 0.9 },
          },
        }}
      />,
    );

    expect(screen.getByText('Selected skill validation')).toBeDefined();
    expect(screen.getByText('45%')).toBeDefined();
    expect(screen.getByText('Skill: weekly-sales-report')).toBeDefined();
    expect(screen.getByText('Missing: kpis, recommendations')).toBeDefined();
    expect(screen.getByText('verification')).toBeDefined();
    expect(screen.getByText('90%')).toBeDefined();
  });
});