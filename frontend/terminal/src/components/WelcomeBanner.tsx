import React from 'react';
import {Box, Text} from 'ink';

import {useTheme} from '../theme/ThemeContext.js';

// ansi256 → hex: colors 18-33 are in the 6×6×6 cube (index = n-16)
// prettier-ignore
const LOGO: Array<[string, string]> = [
	[' ██╗  ██╗  █████╗  ███╗   ██╗ ██████╗  ██╗       █████╗  ███╗   ██╗ ███████╗ ████████╗', '#000087'],
	[' ██║  ██║ ██╔══██╗ ████╗  ██║ ██╔══██╗ ██║      ██╔══██╗ ████╗  ██║ ██╔════╝ ╚══██╔══╝', '#0000af'],
	[' ███████║ ███████║ ██╔██╗ ██║ ██████╔╝ ██║      ███████║ ██╔██╗ ██║ █████╗      ██║   ', '#0000d7'],
	[' ██╔══██║ ██╔══██║ ██║╚██╗██║ ██╔═══╝  ██║      ██╔══██║ ██║╚██╗██║ ██╔══╝      ██║   ', '#0000ff'],
	[' ██║  ██║ ██║  ██║ ██║ ╚████║ ██║      ███████╗ ██║  ██║ ██║ ╚████║ ███████╗    ██║   ', '#005fff'],
	[' ╚═╝  ╚═╝ ╚═╝  ╚═╝ ╚═╝  ╚═══╝ ╚═╝      ╚══════╝ ╚═╝  ╚═╝ ╚═╝  ╚═══╝ ╚══════╝    ╚═╝  ', '#0087ff'],
];

export function WelcomeBanner(): React.JSX.Element {
	const {theme} = useTheme();

	return (
		<Box flexDirection="column" marginBottom={1}>
			<Box flexDirection="column" paddingX={0}>
				{LOGO.map(([line, color], i) => (
					<Text key={i} color={color}>{line}</Text>
				))}
				<Text> </Text>
				<Text>
					<Text dimColor> www.hanplanet.com</Text>
				</Text>
				<Text> </Text>
				<Text>
					<Text dimColor> </Text>
					<Text color={theme.colors.primary}>/help</Text>
					<Text dimColor> commands</Text>
					<Text dimColor>{'  '}|{'  '}</Text>
					<Text color={theme.colors.primary}>/model</Text>
					<Text dimColor> switch</Text>
					<Text dimColor>{'  '}|{'  '}</Text>
					<Text color={theme.colors.primary}>Ctrl+C</Text>
					<Text dimColor> exit</Text>
				</Text>
			</Box>
		</Box>
	);
}
