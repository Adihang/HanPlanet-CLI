import React, {useEffect, useState} from 'react';
import {Text} from 'ink';
import InkSpinner from 'ink-spinner';

import {useTheme} from '../theme/ThemeContext.js';

const VERBS = [
	'Thinking',
	'Processing',
	'Analyzing',
	'Reasoning',
	'Working',
	'Computing',
	'Evaluating',
	'Considering',
];

export function Spinner({label}: {label?: string}): React.JSX.Element {
	const {theme} = useTheme();
	const [verbIndex, setVerbIndex] = useState(0);

	useEffect(() => {
		if (label) return;
		const timer = setInterval(() => {
			setVerbIndex((v) => (v + 1) % VERBS.length);
		}, 3000);
		return () => clearInterval(timer);
	}, [label]);

	const verb = label ?? `${VERBS[verbIndex]}...`;

	// Accent 테마(ASCII 전용)에서는 braille 글자 대신 ink-spinner의 'line' 스피너 사용
	const spinnerType = theme.icons.spinner.length <= 4 ? 'line' : 'dots';

	return (
		<Text>
			<Text color={theme.colors.primary}>
				<InkSpinner type={spinnerType} />
			</Text>
			<Text dimColor> {verb}</Text>
		</Text>
	);
}
