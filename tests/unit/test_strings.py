"""Tests for op_core.strings."""

from __future__ import annotations

from op_core.strings import expand_braces


class TestExpandBraces:
    def test_no_braces(self):
        assert expand_braces('simple') == ['simple']

    def test_comma_list(self):
        assert expand_braces('host{1,2,3}') == ['host1', 'host2', 'host3']

    def test_range(self):
        assert expand_braces('worker{1..4}') == ['worker1', 'worker2', 'worker3', 'worker4']

    def test_prefix_and_suffix(self):
        assert expand_braces('prd{1,2}x') == ['prd1x', 'prd2x']

    def test_range_with_prefix(self):
        assert expand_braces('worker{1..3}.example.com') == [
            'worker1.example.com',
            'worker2.example.com',
            'worker3.example.com',
        ]

    def test_single_value_range(self):
        assert expand_braces('host{5..5}') == ['host5']

    def test_comma_with_multi_char(self):
        assert expand_braces('{master,worker,utility}1') == ['master1', 'worker1', 'utility1']

    def test_no_expansion_literal_braces(self):
        assert expand_braces('host{}') == ['host{}']

    def test_multiple_braces_only_first_expanded(self):
        # Documents a known limitation: the regex is non-recursive,
        # so only the first brace pair is expanded. The second brace pair
        # remains in the suffix of each resulting element.
        assert expand_braces('{a,b}{1,2}') == ['a{1,2}', 'b{1,2}']

    def test_comma_trimming(self):
        assert expand_braces('host{ a , b , c }') == ['hosta', 'hostb', 'hostc']

    def test_empty_string(self):
        assert expand_braces('') == ['']
